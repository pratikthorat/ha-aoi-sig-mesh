"""The IV index jump repair: reported, never applied on its own, adopted when confirmed."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.repairs import async_create_fix_flow
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
