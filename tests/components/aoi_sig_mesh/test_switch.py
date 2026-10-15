"""Plugs and switch-role nodes become switches that send Generic OnOff only."""

from __future__ import annotations

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh.const import ROLE_SWITCH
from custom_components.aoi_sig_mesh.mesh import models
from custom_components.aoi_sig_mesh.mesh.quirks import Quirk
from tests.sim.mesh_sim import SimMesh
from tests.sim.sim_radio import SimRadio

from .conftest import PLUG_MAC, make_entry, node_data, setup_entry, until

PLUG = "switch.kitchen_plug"
DIMMER = "switch.hall_dimmer"
LEVEL_OPCODES = {
    value
    for name, value in vars(models).items()
    if name.startswith(("OP_LIGHTNESS_", "OP_CTL_")) and isinstance(value, int)
}


async def test_a_plug_is_an_outlet_switch(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0)
    radio.advertise(PLUG_MAC)
    coordinator = await setup_entry(
        hass, make_entry(("Kitchen plug", node_data(plug, mac=PLUG_MAC)))
    )
    await until(lambda: coordinator.connected and hass.states.get(PLUG).state == "off")

    record = next(iter(coordinator.nodes.values()))
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: PLUG}, blocking=True
    )
    await until(lambda: record.confirmed.on is True)  # the plug's OnOff Status, not optimism
    assert plug.on
    assert hass.states.get(PLUG).state == "on"
    assert hass.states.get(PLUG).attributes["device_class"] == "outlet"

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: PLUG}, blocking=True
    )
    await until(lambda: record.confirmed.on is False)
    assert not plug.on
    assert hass.states.get(PLUG).state == "off"
    assert hass.states.get("button.kitchen_plug_identify") is None
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_a_dimmable_light_set_to_switch_sends_only_generic_onoff(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    dimmer = sim.add_node("ctl", unicast=0x00C0)  # Lightness + CTL
    # Not Tuya (the sim reports Tuya's CID): no OFF_VIA_ONOFF quirk to hide a Lightness Set 0.
    data = {**node_data(dimmer, mac=PLUG_MAC, role=ROLE_SWITCH), "cid": 0x0059}
    radio.advertise(PLUG_MAC)
    coordinator = await setup_entry(hass, make_entry(("Hall dimmer", data)))
    record = next(iter(coordinator.nodes.values()))
    assert record.quirks == Quirk.NONE
    await until(lambda: coordinator.connected and record.last_seen is not None)
    assert hass.states.async_entity_ids("switch") == [DIMMER]
    assert hass.states.async_entity_ids("light") == []
    dimmer.received.clear()  # the start-up reads use CTL Get

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: DIMMER}, blocking=True
    )
    await until(lambda: record.confirmed.on is True)
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: DIMMER}, blocking=True
    )
    await until(lambda: record.confirmed.on is False)

    opcodes = [opcode for _dst, opcode, _params in dimmer.received]
    assert opcodes == [models.OP_ONOFF_SET, models.OP_ONOFF_SET]
    assert not LEVEL_OPCODES & set(opcodes)
    assert not dimmer.on
    assert hass.states.get(DIMMER).state == "off"
    await hass.config_entries.async_unload(coordinator.entry.entry_id)
