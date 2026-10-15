"""Plugs (Generic OnOff only) become switches; no identify button for them."""

from __future__ import annotations

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant

from tests.sim.mesh_sim import SimMesh
from tests.sim.sim_radio import SimRadio

from .conftest import PLUG_MAC, make_entry, node_data, setup_entry, until

PLUG = "switch.kitchen_plug"


async def test_a_plug_is_an_outlet_switch(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0)
    radio.advertise(PLUG_MAC)
    coordinator = await setup_entry(
        hass, make_entry(("Kitchen plug", node_data(plug, mac=PLUG_MAC)))
    )
    await until(lambda: coordinator.connected and hass.states.get(PLUG).state == "off")

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: PLUG}, blocking=True
    )
    assert plug.on
    assert hass.states.get(PLUG).state == "on"
    assert hass.states.get(PLUG).attributes["device_class"] == "outlet"

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: PLUG}, blocking=True
    )
    assert not plug.on
    assert hass.states.get("button.kitchen_plug_identify") is None
    await hass.config_entries.async_unload(coordinator.entry.entry_id)
