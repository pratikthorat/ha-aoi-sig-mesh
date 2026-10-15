"""Buttons: identify a node, reconnect the network."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AoiConfigEntry
from .coordinator import MeshCoordinator, NodeRecord
from .entity import AoiNetworkEntity, AoiNodeEntity
from .mesh.node import ROLE_HEALTH


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AoiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ReconnectButton(coordinator)])
    for record in coordinator.nodes.values():
        # Plugs without Health Attention get no identify: toggling one could switch off
        # whatever is plugged in.
        if record.node.is_light or ROLE_HEALTH in record.node.models:
            async_add_entities(
                [IdentifyButton(coordinator, record)], config_subentry_id=record.subentry_id
            )


class IdentifyButton(AoiNodeEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord) -> None:
        super().__init__(coordinator, record, "identify")

    async def async_press(self) -> None:
        await self.coordinator.async_identify(self.record.subentry_id)


class ReconnectButton(AoiNetworkEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MeshCoordinator) -> None:
        super().__init__(coordinator, "reconnect")

    async def async_press(self) -> None:
        self.coordinator.async_reconnect()
