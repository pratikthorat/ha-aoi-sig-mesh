"""AoI Bluetooth SIG Mesh: local control of Bluetooth SIG Mesh lights and plugs."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_IV_INDEX_JUMP,
    ISSUE_KEYS_REJECTED,
    ISSUE_NO_PROXY,
    ISSUE_NODE_UNREADABLE,
    ROLE_LIGHT,
)
from .coordinator import MeshCoordinator
from .entity import network_device_info
from .radio import HaBluetoothRadio
from .store import NetworkStore

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.LIGHT, Platform.SENSOR, Platform.SWITCH]

type AoiConfigEntry = ConfigEntry[MeshCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> bool:
    store = NetworkStore(hass, entry.entry_id)
    state = await store.async_load()
    coordinator = MeshCoordinator(hass, entry, HaBluetoothRadio(hass), store, state)
    entry.runtime_data = coordinator
    _async_remove_stale_entities(hass, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))
    await coordinator.async_prime()  # the first seq block is on disk before any link starts
    # node devices point at it with via_device, so it must exist before they are created
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **network_device_info(coordinator)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    if coordinator.needs_reload():
        # Changed while we set up: the update listener skipped it (not loaded yet). The
        # reload waits for this setup to finish.
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> None:
    """Deleting the network deletes its state, TIDs and issues; the address book keeps its own."""
    for issue in (ISSUE_IV_INDEX_JUMP, ISSUE_KEYS_REJECTED, ISSUE_NO_PROXY, ISSUE_NODE_UNREADABLE):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)  # the TID counters
    await NetworkStore(hass, entry.entry_id).async_remove()


@callback
def _async_remove_stale_entities(hass: HomeAssistant, coordinator: MeshCoordinator) -> None:
    """A role change swaps a node's light for a switch (or back): drop the old entity."""
    registry = er.async_get(hass)
    for record in coordinator.nodes.values():
        stale = Platform.SWITCH if record.role == ROLE_LIGHT else Platform.LIGHT
        unique_id = f"{coordinator.network_id}_{record.node.unicast:04x}_{stale}"
        if entity_id := registry.async_get_entity_id(stale, DOMAIN, unique_id):
            registry.async_remove(entity_id)


async def _async_update_listener(hass: HomeAssistant, entry: AoiConfigEntry) -> None:
    """Reload for new or removed nodes, role or capability changes and new options."""
    if entry.state is ConfigEntryState.LOADED and entry.runtime_data.needs_reload():
        hass.config_entries.async_schedule_reload(entry.entry_id)
