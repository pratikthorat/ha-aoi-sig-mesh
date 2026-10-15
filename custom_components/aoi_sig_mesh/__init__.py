"""AoI Bluetooth SIG Mesh: local control of Bluetooth SIG Mesh lights and plugs."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .coordinator import MeshCoordinator
from .radio import HaBluetoothRadio
from .store import NetworkStore

PLATFORMS: list[Platform] = []

type AoiConfigEntry = ConfigEntry[MeshCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> bool:
    store = NetworkStore(hass, entry.entry_id)
    state = await store.async_load()
    coordinator = MeshCoordinator(hass, entry, HaBluetoothRadio(hass), store, state)
    entry.runtime_data = coordinator

    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))
    await coordinator.async_prime()  # the first seq block is on disk before any link starts
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AoiConfigEntry) -> None:
    """Deleting the network deletes its mesh state; the address book keeps its addresses."""
    await NetworkStore(hass, entry.entry_id).async_remove()
