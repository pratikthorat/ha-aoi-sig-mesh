"""Identify (Health Attention on the bulb) and Reconnect buttons."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.aoi_sig_mesh.coordinator import FAILURES_UNAVAILABLE, MeshCoordinator
from custom_components.aoi_sig_mesh.mesh import control
from custom_components.aoi_sig_mesh.mesh.control import BLINKS
from custom_components.aoi_sig_mesh.mesh.models import OP_ONOFF_SET, OP_ONOFF_SET_UNACK
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
    # Acknowledged, so no blink: the bulb was never toggled.
    assert not [r for r in bulb.received if r[1] in (OP_ONOFF_SET, OP_ONOFF_SET_UNACK)]


async def test_a_light_whose_health_is_not_bound_blinks_instead(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "ATTENTION_TIMEOUT", 0.1)
    monkeypatch.setattr(control, "BLINK_INTERVAL", 0.0)
    lamp = sim.add_node("lightness", unicast=0x00C0)
    lamp.bindings.discard((0x00C0, 0x0002))  # set up before Health was bound
    lamp.on = True
    radio.advertise(PLUG_MAC)
    entry = make_entry(("Hall lamp", node_data(lamp, mac=PLUG_MAC)))
    coordinator = await setup_entry(hass, entry)
    try:
        record = next(iter(coordinator.nodes.values()))
        await until(lambda: coordinator.connected and record.confirmed.on is True)
        lamp.received.clear()

        await press(hass, "button.hall_lamp_identify")

        blinks = [r for r in lamp.received if r[1] == OP_ONOFF_SET_UNACK]
        assert [bool(params[0]) for _, _, params in blinks] == [False, True] * BLINKS
        assert lamp.on is True
        assert lamp.attention == 0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unanswered_identify_on_a_plug_does_not_count_toward_unavailable(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """An unbound Health Server drops Attention silently: that says nothing of the link."""
    plug = sim.add_node("onoff", unicast=0x00C0, health=True)
    plug.bindings.discard((0x00C0, 0x0002))  # set up before Health was bound
    radio.advertise(PLUG_MAC)
    entry = make_entry(("Kitchen plug", node_data(plug, mac=PLUG_MAC)))
    with patch.object(control, "ATTENTION_TIMEOUT", 0.05):
        coordinator = await setup_entry(hass, entry)
        try:
            record = next(iter(coordinator.nodes.values()))
            await until(lambda: coordinator.connected and record.confirmed.on is not None)

            for _ in range(FAILURES_UNAVAILABLE):
                with pytest.raises(HomeAssistantError) as err:
                    await press(hass, "button.kitchen_plug_identify")
                assert err.value.translation_key == "command_failed"

            assert record.failures == 0
            assert record.available
            assert plug.on is False  # never toggled
        finally:
            await hass.config_entries.async_unload(entry.entry_id)


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
    try:
        await until(lambda: coordinator.connected)

        assert hass.states.get("button.kitchen_plug_identify") is None
        assert hass.states.get("button.home_mesh_reconnect") is not None
    finally:
        await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_plug_with_health_gets_an_identify_button_that_uses_attention(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0, health=True)
    radio.advertise(PLUG_MAC)
    coordinator = await setup_entry(
        hass, make_entry(("Kitchen plug", node_data(plug, mac=PLUG_MAC)))
    )
    try:
        await until(lambda: coordinator.connected)
        assert hass.states.get("button.kitchen_plug_identify") is not None

        await press(hass, "button.kitchen_plug_identify")

        await until(lambda: plug.attention == 5)
        assert plug.on is False  # never toggled: whatever is plugged in stays as it was
        assert not [r for r in plug.received if r[1] in (OP_ONOFF_SET, OP_ONOFF_SET_UNACK)]
    finally:
        await hass.config_entries.async_unload(coordinator.entry.entry_id)


@pytest.mark.parametrize("on", [True, False])
async def test_a_light_without_health_blinks_and_ends_as_it_was(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, on: bool
) -> None:
    lamp = sim.add_node("lightness", unicast=0x00C0, health=False)
    lamp.on = on
    radio.advertise(PLUG_MAC)
    entry = make_entry(("Hall lamp", node_data(lamp, mac=PLUG_MAC)))
    coordinator = await setup_entry(hass, entry)
    try:
        record = next(iter(coordinator.nodes.values()))
        await until(lambda: coordinator.connected and record.confirmed.on is on)
        lamp.received.clear()

        await press(hass, "button.hall_lamp_identify")

        blinks = [r for r in lamp.received if r[1] == OP_ONOFF_SET_UNACK]
        assert [bool(params[0]) for _, _, params in blinks] == [not on, on] * BLINKS
        assert lamp.on is on
        assert lamp.attention == 0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
