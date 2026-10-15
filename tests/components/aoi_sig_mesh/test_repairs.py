"""The IV index jump repair: reported, never applied on its own, adopted when confirmed."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.mesh.errors import SeqNotSaved
from custom_components.aoi_sig_mesh.repairs import IvIndexJumpFlow, async_create_fix_flow
from custom_components.aoi_sig_mesh.store import NetworkStore
from tests.sim.mesh_sim import SimMesh, SimNode

from .conftest import until


def issue_id(entry: Any) -> str:
    return f"iv_index_jump_{entry.entry_id}"


async def far_jump(hass: HomeAssistant, sim: SimMesh, entry: Any) -> ir.IssueEntry:
    sim.iv_index = 5  # the mesh moved on while we were away
    sim.send_beacon()
    registry = ir.async_get(hass)
    await until(lambda: registry.async_get_issue(DOMAIN, issue_id(entry)) is not None)
    issue = registry.async_get_issue(DOMAIN, issue_id(entry))
    assert issue is not None
    return issue


async def test_a_far_jump_is_reported_as_a_fixable_issue_not_applied(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data

    issue = await far_jump(hass, sim, bulb_entry)

    assert issue.is_fixable
    assert issue.data == {"entry_id": bulb_entry.entry_id}
    assert coordinator.network.iv_index == 0
    assert coordinator.pending_iv_jump is not None


async def test_confirming_adopts_the_jump_saves_it_and_commands_work(
    hass: HomeAssistant,
    sim: SimMesh,
    bulb: SimNode,
    bulb_entry: Any,
    hass_storage: dict[str, Any],
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    issue = await far_jump(hass, sim, bulb_entry)
    flow = await async_create_fix_flow(hass, issue_id(bulb_entry), issue.data)
    flow.hass = hass

    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"] == {"network": "Home mesh", "ours": "0", "theirs": "5"}
    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert coordinator.network.iv_index == 5
    assert coordinator.pending_iv_jump is None
    assert hass_storage[f"{DOMAIN}.{bulb_entry.entry_id}"]["data"]["iv_index"] == 5
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(bulb_entry)) is None
    sid = next(iter(coordinator.nodes))
    await coordinator.async_turn_on(sid)
    assert bulb.on


async def test_the_flow_aborts_when_the_jump_is_gone(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    issue = await far_jump(hass, sim, bulb_entry)
    flow = await async_create_fix_flow(hass, issue_id(bulb_entry), issue.data)
    flow.hass = hass
    await hass.config_entries.async_unload(bulb_entry.entry_id)

    result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "beacon_gone"


async def open_flow(hass: HomeAssistant, entry: Any, issue: ir.IssueEntry) -> IvIndexJumpFlow:
    flow = await async_create_fix_flow(hass, issue_id(entry), issue.data)
    assert isinstance(flow, IvIndexJumpFlow)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    return flow


def held_save() -> tuple[asyncio.Event, asyncio.Event, Any]:
    """Patch target for NetworkStore.async_save that waits for ``release`` before writing."""
    real_save = NetworkStore.async_save
    reached, release = asyncio.Event(), asyncio.Event()

    async def held(self: NetworkStore, state: dict[str, Any]) -> None:
        reached.set()
        await release.wait()
        await real_save(self, state)

    return reached, release, held


async def test_a_newer_jump_before_submit_shows_the_form_again(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    """The user confirmed IV 5; IV 7 is pending now: ask about 7 instead of adopting it."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    flow = await open_flow(hass, bulb_entry, await far_jump(hass, sim, bulb_entry))
    sim.iv_index = 7
    sim.send_beacon()
    await until(lambda: coordinator.pending_iv_jump.iv_index == 7)

    result = await flow.async_step_confirm({})

    assert result["type"] == "form"
    assert result["description_placeholders"]["theirs"] == "7"
    assert coordinator.network.iv_index == 0
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    assert coordinator.network.iv_index == 7


async def test_accepting_a_jump_that_is_no_longer_pending_is_refused(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    await far_jump(hass, sim, bulb_entry)
    shown = coordinator.pending_iv_jump
    assert shown is not None
    sim.iv_index = 7
    sim.send_beacon()
    await until(lambda: coordinator.pending_iv_jump.iv_index == 7)

    with pytest.raises(ValueError, match="no longer pending"):
        await coordinator.async_accept_iv_jump(shown)
    assert coordinator.network.iv_index == 0


async def test_a_newer_jump_during_the_save_keeps_the_issue_and_asks_again(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    flow = await open_flow(hass, bulb_entry, await far_jump(hass, sim, bulb_entry))
    reached, release, held = held_save()

    with patch.object(NetworkStore, "async_save", held):
        submit = hass.async_create_task(flow.async_step_confirm({}))
        await until(reached.is_set)
        sim.iv_index = 7
        sim.send_beacon()
        await until(lambda: coordinator.pending_iv_jump is not None)
        release.set()
        result = await submit

    assert coordinator.network.iv_index == 5
    assert result["type"] == "form"
    assert result["description_placeholders"] == {
        "network": "Home mesh",
        "ours": "5",
        "theirs": "7",
    }
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(bulb_entry)) is not None


async def test_a_send_during_the_accept_write_is_refused(
    hass: HomeAssistant,
    sim: SimMesh,
    bulb: SimNode,
    bulb_entry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seq restarts from zero at the new IV index: nothing goes out before that is on disk."""
    monkeypatch.setattr(coordinator_module, "SAVE_WAIT", 0.3)
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    sid = next(iter(coordinator.nodes))
    flow = await open_flow(hass, bulb_entry, await far_jump(hass, sim, bulb_entry))
    reached, release, held = held_save()
    received = len(bulb.received)

    with patch.object(NetworkStore, "async_save", held):
        submit = hass.async_create_task(flow.async_step_confirm({}))
        try:
            await until(reached.is_set)
            with pytest.raises(HomeAssistantError) as err:
                await coordinator.async_turn_on(sid)
        finally:
            release.set()
            await submit

    assert err.value.translation_key == "seq_not_saved"
    assert len(bulb.received) == received
    assert not bulb.on


async def test_a_failed_write_during_accept_keeps_sends_refused_until_the_retry_lands(
    hass: HomeAssistant,
    sim: SimMesh,
    bulb: SimNode,
    bulb_entry: Any,
    hass_storage: dict[str, Any],
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    key = f"{DOMAIN}.{bulb_entry.entry_id}"
    flow = await open_flow(hass, bulb_entry, await far_jump(hass, sim, bulb_entry))
    real_save = NetworkStore.async_save
    failures = [OSError("disk full")]

    async def flaky(self: NetworkStore, state: dict[str, Any]) -> None:
        if failures:
            raise failures.pop()
        await real_save(self, state)

    with patch.object(NetworkStore, "async_save", flaky):
        await flow.async_step_confirm({})

        assert failures == []
        assert hass_storage[key]["data"]["iv_index"] == 0
        with pytest.raises(SeqNotSaved):
            coordinator.network.next_seq()

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.5))
        await hass.async_block_till_done()

    assert hass_storage[key]["data"]["iv_index"] == 5
    await coordinator.async_turn_on(next(iter(coordinator.nodes)))
    assert bulb.on


async def test_a_beacon_after_adoption_does_not_bring_the_issue_back(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    """Proxies keep repeating the beacon that was adopted."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    flow = await open_flow(hass, bulb_entry, await far_jump(hass, sim, bulb_entry))
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"

    with patch.object(coordinator, "_on_heard", wraps=coordinator._on_heard) as heard:
        sim.send_beacon()
        await until(lambda: heard.called)

    assert coordinator.pending_iv_jump is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(bulb_entry)) is None


async def test_the_flow_clears_the_issue_of_a_deleted_network(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        "iv_index_jump_gone",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        data={"entry_id": "gone"},
        translation_key="iv_index_jump",
    )
    flow = IvIndexJumpFlow("gone")
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "beacon_gone"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "iv_index_jump_gone") is None


async def test_deleting_the_network_deletes_its_issues(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    await far_jump(hass, sim, bulb_entry)
    others = ("no_proxy", "keys_rejected", "node_unreadable")
    for kind in others:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{kind}_{bulb_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
        )

    await hass.config_entries.async_remove(bulb_entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    for kind in ("iv_index_jump", *others):
        assert registry.async_get_issue(DOMAIN, f"{kind}_{bulb_entry.entry_id}") is None
