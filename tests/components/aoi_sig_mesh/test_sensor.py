"""Diagnostic sensors: signal strength (with its age), connected via, last seen."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.sensor import ATTR_AGE, SignalStrengthSensor
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import BULB_MAC, SimRadio

from .conftest import NETWORK_ID, make_entry, node_data, setup_entry, until

SIGNAL = "sensor.home_mesh_signal_strength"
VIA = "sensor.home_mesh_connected_via"
FOREIGN = "AA:BB:CC:00:00:01"  # a proxy that is not one of our nodes


async def test_signal_strength_follows_the_proxy_adverts(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    assert hass.states.get(SIGNAL).state == "-60"

    radio.advertise(rssi=-71)
    await hass.async_block_till_done()

    state = hass.states.get(SIGNAL)
    assert state.state == "-71"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["age"] == 0


async def test_signal_strength_keeps_the_last_value_and_shows_its_age(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    """While connected the proxy stops advertising; the last RSSI stays, with its age."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    radio.forget()
    coordinator.rssi_seen -= 125  # the advert was heard two minutes ago

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    state = hass.states.get(SIGNAL)
    assert state.state == "-60"
    assert state.attributes["age"] >= 125


async def test_age_is_not_recorded() -> None:
    assert ATTR_AGE in SignalStrengthSensor._unrecorded_attributes


async def test_connected_via_names_our_own_node(hass: HomeAssistant, bulb_entry: Any) -> None:
    state = hass.states.get(VIA)

    assert state.state == "Desk lamp"
    assert state.attributes["address"] == BULB_MAC


async def test_last_seen_is_disabled_by_default(hass: HomeAssistant, bulb_entry: Any) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{NETWORK_ID}_00b0_last_seen"
    )

    assert entity_id is not None
    assert (
        er.async_get(hass).async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get(entity_id) is None


async def _enable_last_seen(hass: HomeAssistant, entry: Any) -> str:
    """Enable the (disabled by default) last-seen sensor and reload so it is added."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{NETWORK_ID}_00b0_last_seen")
    registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entity_id


async def test_last_seen_reports_when_the_node_was_last_heard(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    started = dt_util.utcnow()
    entity_id = await _enable_last_seen(hass, bulb_entry)
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))

    await until(lambda: record.last_seen is not None and hass.states.get(entity_id).state)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes["device_class"] == "timestamp"
    assert dt_util.parse_datetime(state.state) == record.last_seen.replace(microsecond=0)
    assert record.last_seen >= started


async def test_last_seen_stays_available_while_the_node_is_unavailable(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "LINK_DOWN_GRACE", 0.1)
    entity_id = await _enable_last_seen(hass, bulb_entry)
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    await until(lambda: record.last_seen is not None)
    heard = record.last_seen

    radio.forget()
    radio.transports[-1].drop()
    await until(lambda: not record.available)
    await hass.async_block_till_done()

    assert hass.states.get("light.desk_lamp").state == "unavailable"
    shown = dt_util.parse_datetime(hass.states.get(entity_id).state)
    assert shown == heard.replace(microsecond=0)  # a timestamp state is shown to the second


async def test_connected_via_clears_on_disconnect(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    radio.forget()
    radio.transports[-1].drop()
    await until(lambda: not coordinator.connected)

    await until(lambda: hass.states.get(VIA).state == "unknown")
    assert hass.states.get(VIA).attributes["address"] is None


@pytest.mark.parametrize(("name", "shown"), [("Hall proxy", "Hall proxy"), (None, FOREIGN)])
async def test_connected_via_a_foreign_proxy_shows_its_name_else_its_mac(
    hass: HomeAssistant,
    sim: SimMesh,
    bulb: SimNode,
    radio: SimRadio,
    name: str | None,
    shown: str,
) -> None:
    """A proxy that is not one of our nodes: its advertised name, or its MAC without one."""
    radio.advertise(FOREIGN, name=name)
    entry = make_entry(("Desk lamp", node_data(bulb)))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected)
    await hass.async_block_till_done()

    state = hass.states.get(VIA)
    assert state.state == shown
    assert state.attributes["address"] == FOREIGN
    await hass.config_entries.async_unload(entry.entry_id)
