"""End to end: the Mesh CW bulb moves from tuya_ble_mesh and works, without a reset."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aoi_sig_mesh.const import DOMAIN, TUYA_DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator, held_networks
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import NETWORK_ID, until

LAMP = "light.mesh_cw"
OURS = 0x7F00


def tuya_source(hass: HomeAssistant, bulb: SimNode) -> MockConfigEntry:
    source = MockConfigEntry(
        domain=TUYA_DOMAIN,
        title="Mesh CW",
        data={
            "device_type": "sig_plug",
            "mac_address": "dc:23:50:00:00:19",
            "unicast_target": "00B0",
            "unicast_our": "0001",
            "iv_index": 0,
            "net_key": NET_KEY.hex(),
            "app_key": APP_KEY.hex(),
            "dev_key": bulb.dev_key.hex(),
            "sig_models": {
                "cid": 0x07D0,
                "pid": 0x0280,
                "vid": 0x3737,
                "elements": 2,
                "onoff": 0xB0,
                "lightness": 0xB0,
                "ctl": 0xB0,
                "ctl_temp": 0xB1,
                "temp_range": [2700, 6500],
            },
        },
    )
    source.add_to_hass(hass)
    return source


async def run_import(hass: HomeAssistant, source: MockConfigEntry, *, disable_old: bool) -> Any:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tuya_import"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_entry": source.entry_id, "name": "Home mesh"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"disable_old": disable_old}
    )


async def test_the_bulb_migrates_and_works(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    bulb = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    bulb.kelvin_min, bulb.kelvin_max = 3000, 5700  # Tuya stored the generic 2700-6500
    radio.advertise()
    source = tuya_source(hass, bulb)
    # The fork's light holds the id every dashboard, automation and voice alias points at.
    registry = er.async_get(hass)
    fork = registry.async_get_or_create(
        LIGHT_DOMAIN,
        TUYA_DOMAIN,
        "dc:23:50:00:00:19",
        config_entry=source,
        suggested_object_id="mesh_cw",
    )
    assert fork.entity_id == LAMP

    made: list[MeshCoordinator] = []  # one per setup: the first, then each reload

    def counted(*args: Any, **kwargs: Any) -> MeshCoordinator:
        made.append(MeshCoordinator(*args, **kwargs))
        return made[-1]

    with patch("custom_components.aoi_sig_mesh.MeshCoordinator", side_effect=counted):
        result = await run_import(hass, source, disable_old=True)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        entry = result["result"]
        first = entry.runtime_data
        # Imported unconfigured: the first connect must do the AppKey and bindings.
        assert next(iter(entry.subentries.values())).data["configured"] is False

        # First connect: AppKey, bindings, publications to us, Composition and the real range.
        await until(lambda: next(iter(entry.subentries.values())).data["configured"] is True)
        await until(lambda: entry.runtime_data is not first and entry.runtime_data.connected)
        await hass.async_block_till_done()

    assert source.disabled_by is ConfigEntryDisabler.USER
    # The ids moved across: ours took light.mesh_cw, the fork's light stepped aside.
    ours = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}_00b0_light")
    assert ours == LAMP
    assert registry.async_get(fork.id).entity_id == "light.mesh_cw_tuya_ble_mesh"
    assert bulb.has_app_key
    assert bulb.publications and set(bulb.publications.values()) == {OURS}
    state = hass.states.get(LAMP)
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 3000
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 5700

    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: LAMP, ATTR_BRIGHTNESS: 64}, blocking=True
    )
    assert bulb.on and bulb.lightness == 64 * 257
    assert hass.states.get(LAMP).state == "on"  # so "off" below is the wall switch's doing

    sim.local_change(bulb, False, dst=OURS)  # the wall switch, published to us
    await until(lambda: hass.states.get(LAMP).state == "off")
    assert made == [first, entry.runtime_data]  # set up, then reloaded exactly once

    # Rollback: deleting our network hands light.mesh_cw back to the fork's light.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(fork.id).entity_id == LAMP
    assert registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}_00b0_light") is None


@pytest.mark.parametrize("disable_old", [True, False])
async def test_we_connect_only_once_the_old_entry_lets_go_of_the_bulb(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, disable_old: bool
) -> None:
    """The bulb takes one Bluetooth connection. While the old entry holds it ours cannot
    connect, and the old entry's unload drops ours mid-setup. Unless both are kept on."""
    bulb = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    source = tuya_source(hass, bulb)
    seen: list[ConfigEntryDisabler | None] = []
    real_start = MeshCoordinator.start

    @callback
    def start(self: MeshCoordinator) -> None:
        seen.append(source.disabled_by)
        real_start(self)

    with patch.object(MeshCoordinator, "start", start):
        result = await run_import(hass, source, disable_old=disable_old)
        await until(lambda: result["result"].runtime_data.connected)

    assert seen[0] is (ConfigEntryDisabler.USER if disable_old else None)
    assert not held_networks(hass)


async def test_a_held_network_that_failed_to_set_up_connects_on_its_retry(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    bulb = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    source = tuya_source(hass, bulb)
    with patch(
        "custom_components.aoi_sig_mesh.NetworkStore.async_load", side_effect=ConfigEntryNotReady
    ):
        result = await run_import(hass, source, disable_old=True)
    entry = result["result"]
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not held_networks(hass)

    await hass.config_entries.async_reload(entry.entry_id)

    await until(lambda: entry.runtime_data.connected)
