"""Diagnostic sensors: signal strength and proxy of the network, last seen of each node."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AoiConfigEntry
from .coordinator import MeshCoordinator, NodeRecord
from .entity import AoiNetworkEntity, AoiNodeEntity

ATTR_AGE = "age"
ATTR_ADDRESS = "address"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AoiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([SignalStrengthSensor(coordinator), ConnectedViaSensor(coordinator)])
    for record in coordinator.nodes.values():
        async_add_entities(
            [LastSeenSensor(coordinator, record)], config_subentry_id=record.subentry_id
        )


class SignalStrengthSensor(AoiNetworkEntity, SensorEntity):
    """RSSI of the proxy we are connected through (the last one heard; its age attached)."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({ATTR_AGE})

    def __init__(self, coordinator: MeshCoordinator) -> None:
        super().__init__(coordinator, "signal_strength")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.rssi

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        age = self.coordinator.rssi_age
        return {ATTR_AGE: None if age is None else round(age)}


class ConnectedViaSensor(AoiNetworkEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MeshCoordinator) -> None:
        super().__init__(coordinator, "connected_via")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.connected_via

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        proxy = self.coordinator.manager.proxy
        return {ATTR_ADDRESS: None if proxy is None else proxy.address}


class LastSeenSensor(AoiNodeEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "last_seen"

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord) -> None:
        super().__init__(coordinator, record, "last_seen")

    @property
    def available(self) -> bool:
        return True  # when it was last heard matters most while it is unavailable

    @property
    def native_value(self) -> datetime | None:
        return self.record.last_seen
