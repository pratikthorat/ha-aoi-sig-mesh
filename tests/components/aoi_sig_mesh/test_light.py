"""The light entity against the simulated Mesh CW bulb (Tuya quirks on)."""

from __future__ import annotations

from typing import Any

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
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.aoi_sig_mesh.light import (
    brightness_to_lightness,
    lightness_to_brightness,
)
from custom_components.aoi_sig_mesh.mesh.models import OP_ONOFF_SET
from custom_components.aoi_sig_mesh.mesh.node import KELVIN_DEFAULT_MAX, KELVIN_DEFAULT_MIN
from tests.sim.mesh_sim import SimMesh, SimNode

from .conftest import until

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
