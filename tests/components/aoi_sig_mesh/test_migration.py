"""End to end: the Mesh CW bulb moves from tuya_ble_mesh and works, without a reset."""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryDisabler
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aoi_sig_mesh.const import DOMAIN, TUYA_DOMAIN
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimMesh
from tests.sim.sim_radio import SimRadio

from .conftest import until

LAMP = "light.mesh_cw"
OURS = 0x7F00


async def test_the_bulb_migrates_and_works(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    bulb = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    bulb.kelvin_min, bulb.kelvin_max = 3000, 5700  # Tuya stored the generic 2700-6500
    radio.advertise()
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

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tuya_import"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_entry": source.entry_id, "name": "Home mesh"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"disable_old": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    first = entry.runtime_data

    # First connect: AppKey, bindings, publications to us, Composition and the real range.
    await until(lambda: next(iter(entry.subentries.values())).data["configured"] is True)
    await until(lambda: entry.runtime_data is not first and entry.runtime_data.connected)
    await hass.async_block_till_done()

    assert source.disabled_by is ConfigEntryDisabler.USER
    assert bulb.has_app_key
    assert bulb.publications and set(bulb.publications.values()) == {OURS}
    state = hass.states.get(LAMP)
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 3000
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 5700

    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: LAMP, ATTR_BRIGHTNESS: 64}, blocking=True
    )
    assert bulb.on and bulb.lightness == 64 * 257

    sim.local_change(bulb, False, dst=OURS)  # the wall switch, published to us
    await until(lambda: hass.states.get(LAMP).state == "off")
    await hass.config_entries.async_unload(entry.entry_id)
