"""Availability: no proxy, unanswered polls, link drops, and follow-up reads."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import POLL_INTERVAL, MeshCoordinator
from custom_components.aoi_sig_mesh.mesh.models import OP_ONOFF_GET, OP_ONOFF_STATUS, encode_opcode
from tests.sim.mesh_sim import CLIENT, SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import make_entry, node_data, setup_entry, until


async def test_no_proxy_makes_nodes_unavailable_then_raises_an_issue(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "LINK_DOWN_GRACE", 0.1)
    monkeypatch.setattr(coordinator_module, "NO_PROXY_ISSUE_AFTER", 0.3)
    coordinator = await setup_entry(hass, make_entry(("Desk lamp", node_data(bulb))))
    record = next(iter(coordinator.nodes.values()))
    issue_id = f"no_proxy_{coordinator.entry.entry_id}"
    registry = ir.async_get(hass)

    await until(lambda: not record.available)
    await until(lambda: registry.async_get_issue(DOMAIN, issue_id) is not None)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.translation_placeholders == {"network": "Home mesh", "minutes": "0"}

    radio.advertise()
    await until(lambda: record.available and coordinator.connected)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_three_unanswered_polls_make_the_node_unavailable(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    sim.silent = True

    for polls in range(1, 4):
        assert record.available
        async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL * polls)
        await until(lambda polls=polls: record.failures >= polls)
        await hass.async_block_till_done()

    assert not record.available
    sim.silent = False
    sim.local_change(bulb, True)  # anything heard from it makes it available again
    await until(lambda: record.available and record.failures == 0)


async def test_a_dropped_link_keeps_nodes_available_for_the_grace_period(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "LINK_DOWN_GRACE", 0.3)
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    radio.forget()
    radio.transports[-1].drop()
    await until(lambda: not coordinator.connected)

    assert record.available
    await until(lambda: not record.available)

    radio.advertise()
    await until(lambda: record.available)
    assert len(radio.connects) >= 2


async def test_a_drop_reconnects_before_anyone_notices(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))

    radio.transports[-1].drop()

    await until(lambda: len(radio.transports) == 2 and coordinator.connected)
    assert record.available
    assert not radio.transports[0].is_connected


async def test_a_status_with_remaining_time_reads_the_final_state_afterwards(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    # OnOff Status: present off, target on, 500 ms remaining (5 steps of 100 ms).
    sim.send_access(bulb.unicast, CLIENT, encode_opcode(OP_ONOFF_STATUS) + bytes([0, 1, 0x05]))
    await until(lambda: record.state.on is True)
    received = len(bulb.received)

    # The read comes 1 s later (the transition plus FOLLOW_UP_MARGIN), not before. (The
    # helper adds 0.5 s of its own, so this fires everything due in the next 0.6 s.)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=0.1))
    await hass.async_block_till_done()
    assert len(bulb.received) == received

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))

    await until(lambda: len(bulb.received) > received)
    assert bulb.received[received][1] == OP_ONOFF_GET


async def test_a_poll_is_skipped_while_the_last_round_still_runs(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    """A slow round (silent nodes, many nodes) must not pile up a second one behind it."""
    calls: list[int] = []
    release = asyncio.Event()

    async def blocked(_self: Any) -> int:
        calls.append(1)
        await release.wait()
        return 1

    with patch.object(coordinator_module.NodeCommands, "refresh", blocked):
        async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
        await until(lambda: calls)
        async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL * 2)
        await hass.async_block_till_done()
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(calls) == 1
