"""Identify (Health Attention on the bulb) and Reconnect buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import PLUG_MAC, make_entry, node_data, setup_entry, until


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_identify_sets_health_attention(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    await press(hass, "button.desk_lamp_identify")

    await until(lambda: bulb.attention == 5)


async def test_reconnect_opens_a_new_connection(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data

    await press(hass, "button.home_mesh_reconnect")

    await until(lambda: len(radio.transports) == 2 and coordinator.connected)
    assert not radio.transports[0].is_connected


async def test_plug_without_health_has_no_identify_button(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0)
    radio.advertise(PLUG_MAC)
    coordinator = await setup_entry(
        hass, make_entry(("Kitchen plug", node_data(plug, mac=PLUG_MAC)))
    )
    await until(lambda: coordinator.connected)

    assert hass.states.get("button.kitchen_plug_identify") is None
    assert hass.states.get("button.home_mesh_reconnect") is not None
    await hass.config_entries.async_unload(coordinator.entry.entry_id)
