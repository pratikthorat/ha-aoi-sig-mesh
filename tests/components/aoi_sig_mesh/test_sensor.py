"""Diagnostic sensors: signal strength (with its age), connected via, last seen."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.sensor import ATTR_AGE, SignalStrengthSensor
from tests.sim.sim_radio import BULB_MAC, SimRadio

from .conftest import NETWORK_ID

SIGNAL = "sensor.home_mesh_signal_strength"
VIA = "sensor.home_mesh_connected_via"


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
