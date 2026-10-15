"""Repair flows: adopting an IV index jump the mesh announced."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_IV_INDEX_JUMP
from .coordinator import MeshCoordinator
from .mesh.pdu import SecureBeacon


class IvIndexJumpFlow(RepairsFlow):
    """Confirm, then adopt the pending beacon's IV index (seq restarts from zero)."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._shown: SecureBeacon | None = None  # the jump the form asked about

    def _coordinator(self) -> MeshCoordinator | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:  # the network was deleted: nothing would ever clear its issue
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_IV_INDEX_JUMP}_{self._entry_id}")
            return None
        if entry.state is not ConfigEntryState.LOADED:
            return None
        coordinator: MeshCoordinator = entry.runtime_data
        return coordinator if coordinator.pending_iv_jump is not None else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        coordinator = self._coordinator()
        if coordinator is None or coordinator.pending_iv_jump is None:
            return self.async_abort(reason="beacon_gone")
        if user_input is not None and coordinator.pending_iv_jump == self._shown:
            await coordinator.async_accept_iv_jump(coordinator.pending_iv_jump)
            if coordinator.pending_iv_jump is None:
                return self.async_create_entry(data={})
            # A newer jump came in during the save: finishing would delete its issue.
        # Not the jump the user saw (or a newer one arrived): ask about this one.
        pending = coordinator.pending_iv_jump
        self._shown = pending
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "network": coordinator.entry.title,
                "ours": str(coordinator.network.iv_index),
                "theirs": str(pending.iv_index),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    if issue_id.startswith(f"{ISSUE_IV_INDEX_JUMP}_") and data is not None:
        return IvIndexJumpFlow(str(data["entry_id"]))
    raise ValueError(f"unknown repair {issue_id}")
