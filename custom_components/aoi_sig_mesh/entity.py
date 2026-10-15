"""Base entities: the network device and one device per node."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER, MANUFACTURERS
from .coordinator import MeshCoordinator, NodeRecord


def network_device_info(coordinator: MeshCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.network_id)},
        name=coordinator.entry.title,
        manufacturer=MANUFACTURER,
        model="Bluetooth SIG Mesh network",
        entry_type=DeviceEntryType.SERVICE,
    )


def node_device_info(coordinator: MeshCoordinator, record: NodeRecord) -> DeviceInfo:
    """No Bluetooth ``connections``: they would merge this device with the tuya_ble_mesh one."""
    node = record.node
    manufacturer = None
    if node.cid is not None:
        manufacturer = MANUFACTURERS.get(node.cid, f"CID 0x{node.cid:04X}")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{coordinator.network_id}_{node.unicast:04x}")},
        name=record.title,
        manufacturer=manufacturer,
        model=None if node.pid is None else f"PID 0x{node.pid:04X}",
        sw_version=None if node.vid is None else f"VID 0x{node.vid:04X}",
        via_device=(DOMAIN, coordinator.network_id),
    )


class AoiEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _listener_key: str | None = None

    def __init__(self, coordinator: MeshCoordinator) -> None:
        self.coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self._listener_key, self.async_write_ha_state)
        )


class AoiNetworkEntity(AoiEntity):
    """An entity of the network's service device."""

    def __init__(self, coordinator: MeshCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.network_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = network_device_info(coordinator)


class AoiNodeEntity(AoiEntity):
    """An entity of one node's device; unavailable when the node stops answering."""

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord, key: str) -> None:
        super().__init__(coordinator)
        self.record = record
        self._listener_key = record.subentry_id
        self._attr_unique_id = f"{coordinator.network_id}_{record.node.unicast:04x}_{key}"
        self._attr_device_info = node_device_info(coordinator, record)

    @property
    def available(self) -> bool:
        return self.record.available
