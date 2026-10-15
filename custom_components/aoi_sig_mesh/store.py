"""Persistence: per-network mesh state (IV, seq, replay) and the provisioner address book."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class _StrictStore(Store[dict[str, Any]]):
    """A Store whose save reports failure and never defers.

    The public ``Store.async_save`` cannot be used for the mesh state: it logs and swallows
    ``WriteError``/``SerializationError`` (so a failed write looks like a success), and while
    Home Assistant is stopping it only registers a final-write listener and returns before
    anything is on disk. Either would let a seq be confirmed that was never saved.
    """

    async def async_save_strict(self, data: dict[str, Any]) -> None:
        """Write ``data`` now. Raise if it could not be written; return only once it is on disk."""
        envelope = {
            "version": self.version,
            "minor_version": self.minor_version,
            "key": self.key,
            "data": data,
        }
        async with self._write_lock:
            self._manager.async_invalidate(self.key)
            await self._async_write_data(envelope)


class NetworkStore:
    """``.storage/aoi_sig_mesh.<entry_id>``: what MeshNetwork.persistent_state() returns."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _StrictStore(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True
        )

    async def async_load(self) -> dict[str, Any] | None:
        return await self._store.async_load()

    async def async_save(self, state: dict[str, Any]) -> None:
        await self._store.async_save_strict(state)

    async def async_remove(self) -> None:
        await self._store.async_remove()


class StateSaver:
    """Writes mesh states one at a time, in the order given, and confirms each one.

    MeshNetwork runs with ``persist_is_durable=False``: ``save`` is its ``on_persist``. Each
    write is awaited, then ``on_saved`` gets that exact state (``MeshNetwork.mark_persisted``).
    ``NetworkStore.async_save`` bypasses two ``Store`` behaviours that would make a confirmation
    untrue: it raises on a failed write (``Store`` only logs it) and it writes immediately even
    while Home Assistant is stopping (``Store`` defers to a final-write listener). So ``on_saved``
    runs only after the write returned without error. A failed write is logged, never
    confirmed, and reported to ``on_failed`` (which schedules a retry). Writes run in tasks that
    start on the next loop iteration, so ``on_failed`` never runs inside ``save``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: NetworkStore,
        on_saved: Callable[[dict[str, Any]], None],
        on_failed: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._store = store
        self._on_saved = on_saved
        self._on_failed = on_failed
        self._lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    @callback
    def save(self, state: dict[str, Any]) -> None:
        """Queue a write of ``state``. Safe to call from a callback (it never blocks)."""
        task = self._hass.async_create_task(self._write(state), eager_start=False)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _write(self, state: dict[str, Any]) -> None:
        async with self._lock:  # asyncio.Lock wakes waiters in FIFO order
            try:
                await self._store.async_save(state)
            except Exception:
                _LOGGER.exception("Saving the mesh state failed")
                self._on_failed()
                return
            self._on_saved(state)

    async def async_flush(self) -> None:
        """Wait until every queued write has finished."""
        while pending := [task for task in self._pending if not task.done()]:
            await asyncio.wait(pending)


class AddressBook:
    """``.storage/aoi_sig_mesh.addresses``: provisioner addresses we have sent from.

    Keyed by Network ID (hex). Kept when an entry is deleted, so importing the same network
    again never reuses an address whose seq values were already spent.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, list[int]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.addresses", atomic_writes=True
        )
        self._lock: asyncio.Lock = hass.data.setdefault(f"{DOMAIN}_address_lock", asyncio.Lock())

    async def async_used(self, network_id: str) -> list[tuple[int, int]]:
        data = await self._store.async_load() or {}
        return [(address, 1) for address in data.get(network_id, [])]

    async def async_add(self, network_id: str, address: int) -> None:
        async with self._lock:
            data = await self._store.async_load() or {}
            used = set(data.get(network_id, []))
            used.add(address)
            data[network_id] = sorted(used)
            await self._store.async_save(data)
