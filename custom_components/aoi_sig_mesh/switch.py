"""Switches: plugs (Generic OnOff only), or any node whose role is set to switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AoiConfigEntry
from .const import ROLE_SWITCH
from .coordinator import MeshCoordinator, NodeRecord
from .entity import AoiNodeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AoiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for record in coordinator.nodes.values():
        if record.role == ROLE_SWITCH:
            async_add_entities(
                [AoiSwitch(coordinator, record)], config_subentry_id=record.subentry_id
            )


class AoiSwitch(AoiNodeEntity, SwitchEntity):
    _attr_name = None
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord) -> None:
        super().__init__(coordinator, record, "switch")

    @property
    def is_on(self) -> bool | None:
        return self.record.state.on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_on(self.record.subentry_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off(self.record.subentry_id)
