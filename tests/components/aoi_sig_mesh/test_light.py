"""The light entity against the simulated Mesh CW bulb (Tuya quirks on)."""

from __future__ import annotations

import struct
from typing import Any

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ColorMode,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.aoi_sig_mesh.const import ROLE_LIGHT
from custom_components.aoi_sig_mesh.light import (
    brightness_to_lightness,
    lightness_to_brightness,
)
from custom_components.aoi_sig_mesh.mesh.models import (
    OP_CTL_TEMP_SET,
    OP_CTL_TEMP_SET_UNACK,
    OP_ONOFF_SET,
)
from custom_components.aoi_sig_mesh.mesh.node import KELVIN_DEFAULT_MAX, KELVIN_DEFAULT_MIN
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import make_entry, node_data, setup_entry, until

LAMP = "light.desk_lamp"


async def turn(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: LAMP, **data}, blocking=True
    )


def test_brightness_scales_round_trip() -> None:
    assert brightness_to_lightness(255) == 0xFFFF
    assert brightness_to_lightness(1) == 257
    assert lightness_to_brightness(0xFFFF) == 255
    assert lightness_to_brightness(1) == 1  # on but very dim is never brightness 0
    assert all(lightness_to_brightness(brightness_to_lightness(b)) == b for b in range(1, 256))


async def test_the_bulb_is_a_tunable_white_light(hass: HomeAssistant, bulb_entry: Any) -> None:
    state = hass.states.get(LAMP)

    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.COLOR_TEMP]
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == KELVIN_DEFAULT_MIN
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == KELVIN_DEFAULT_MAX


async def test_brightness_while_off_turns_the_bulb_on_first(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """Tuya ignores a Lightness Set while off; the bulb must still end up on and dimmed."""
    assert not bulb.on

    await turn(hass, SERVICE_TURN_ON, **{ATTR_BRIGHTNESS: 128})

    assert bulb.on
    assert bulb.lightness == 128 * 257
    state = hass.states.get(LAMP)
    assert state.state == "on"
    assert state.attributes[ATTR_BRIGHTNESS] == 128


async def test_color_temperature_goes_to_the_second_element(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    await turn(hass, SERVICE_TURN_ON)

    await turn(hass, SERVICE_TURN_ON, **{ATTR_COLOR_TEMP_KELVIN: 3000})

    assert bulb.kelvin == 3000
    assert bulb.received[-1][0] == bulb.unicast + 1
    state = hass.states.get(LAMP)
    assert state.attributes[ATTR_COLOR_MODE] == ColorMode.COLOR_TEMP
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 3000


async def test_off_uses_onoff_with_the_transition(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """Lightness 0 does not turn the Tuya bulb off; Generic OnOff Set does."""
    await turn(hass, SERVICE_TURN_ON)

    await turn(hass, SERVICE_TURN_OFF, **{ATTR_TRANSITION: 2})

    assert not bulb.on
    _dst, opcode, params = bulb.received[-1]
    assert opcode == OP_ONOFF_SET
    assert params[0] == 0
    assert params[2] == 20  # 20 steps of 100 ms
    assert hass.states.get(LAMP).state == "off"


async def test_a_wall_switch_change_shows_up_in_home_assistant(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, bulb_entry: Any
) -> None:
    sim.local_change(bulb, True)

    await until(lambda: hass.states.get(LAMP).state == "on")


async def test_node_devices_have_no_bluetooth_connections(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    """Connections would merge the device with the old tuya_ble_mesh one on migration."""
    registry = dr.async_get(hass)
    entity = er.async_get(hass).async_get(LAMP)
    device = registry.async_get(entity.device_id)

    assert device.connections == set()
    assert device.via_device_id is not None
    assert registry.async_get(device.via_device_id).entry_type is dr.DeviceEntryType.SERVICE


def first_record(entry: Any) -> Any:
    return next(iter(entry.runtime_data.nodes.values()))


async def test_unique_id_is_network_unicast_and_key(hass: HomeAssistant, bulb_entry: Any) -> None:
    entity = er.async_get(hass).async_get(LAMP)

    assert entity.unique_id == f"{bulb_entry.runtime_data.network_id}_00b0_light"


@pytest.mark.parametrize(
    ("kind", "modes", "brightness"),
    [("lightness", [ColorMode.BRIGHTNESS], 128), ("onoff", [ColorMode.ONOFF], None)],
)
async def test_non_tunable_nodes_get_a_simpler_colour_mode(
    hass: HomeAssistant,
    sim: SimMesh,
    radio: SimRadio,
    kind: str,
    modes: list[ColorMode],
    brightness: int | None,
) -> None:
    node = sim.add_node(kind, unicast=0x00C0)
    radio.advertise()
    entry = make_entry(("Side lamp", node_data(node, role=ROLE_LIGHT)))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected and node.received)
    await until(lambda: first_record(entry).last_seen)
    await turn_entity(
        hass,
        "light.side_lamp",
        SERVICE_TURN_ON,
        **({} if brightness is None else {ATTR_BRIGHTNESS: brightness}),
    )

    state = hass.states.get("light.side_lamp")
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == modes
    assert state.state == "on"
    assert state.attributes.get(ATTR_BRIGHTNESS) == brightness
    await hass.config_entries.async_unload(entry.entry_id)


async def turn_entity(hass: HomeAssistant, entity_id: str, service: str, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


async def test_node_device_identity_comes_from_the_composition(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    record = first_record(bulb_entry)
    node = record.node
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(LAMP).device_id)

    assert device.manufacturer == "Tuya"
    assert device.model == f"PID 0x{node.pid:04X}"
    assert device.sw_version == f"VID 0x{node.vid:04X}"


async def test_an_unknown_company_id_is_shown_as_hex(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio
) -> None:
    entry = make_entry(("Desk lamp", {**node_data(bulb), "cid": 0x1234}))
    await setup_entry(hass, entry)

    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(LAMP).device_id)
    assert device.manufacturer == "CID 0x1234"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_color_temperature_above_the_range_is_clamped(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    await turn(hass, SERVICE_TURN_ON)

    await turn(hass, SERVICE_TURN_ON, **{ATTR_COLOR_TEMP_KELVIN: KELVIN_DEFAULT_MAX + 2000})

    # The sim clamps on receipt too, so check what the integration put on the wire.
    sets = [
        params
        for _dst, opcode, params in bulb.received
        if opcode in (OP_CTL_TEMP_SET, OP_CTL_TEMP_SET_UNACK)
    ]
    assert struct.unpack_from("<H", sets[-1])[0] == KELVIN_DEFAULT_MAX
    assert bulb.kelvin == KELVIN_DEFAULT_MAX
    state = hass.states.get(LAMP)
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == KELVIN_DEFAULT_MAX


async def test_availability_follows_the_node_record(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator = bulb_entry.runtime_data
    record = first_record(bulb_entry)
    assert hass.states.get(LAMP).state != STATE_UNAVAILABLE

    for _ in range(3):
        coordinator._failed(record)
    await hass.async_block_till_done()

    assert hass.states.get(LAMP).state == STATE_UNAVAILABLE
    record.available = True
    coordinator._notify(record.subentry_id)
    await hass.async_block_till_done()
    assert hass.states.get(LAMP).state != STATE_UNAVAILABLE


async def test_the_listener_is_removed_on_entity_removal_and_unload(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator = bulb_entry.runtime_data
    key = first_record(bulb_entry).subentry_id
    assert len(coordinator._listeners[key]) == 2  # the light and its identify button

    er.async_get(hass).async_remove(LAMP)
    await hass.async_block_till_done()
    assert len(coordinator._listeners[key]) == 1  # only the identify button is left


async def test_the_listener_is_removed_when_the_entry_unloads(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator = bulb_entry.runtime_data
    key = first_record(bulb_entry).subentry_id
    assert len(coordinator._listeners[key]) == 2

    await hass.config_entries.async_unload(bulb_entry.entry_id)

    assert bulb_entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator._listeners[key] == []
