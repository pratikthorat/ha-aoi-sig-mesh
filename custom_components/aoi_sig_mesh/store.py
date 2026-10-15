"""Persistence: per-network mesh state (IV, seq, replay) and the provisioner address book."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .mesh.node import allocate_unicast

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
            # Like Store._async_handle_write_data: no held payload may outlive a write, or the
            # next async_load on this instance returns it instead of what is on disk.
            self._data = None


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


NODES_KEY = "nodes"


class AddressBook:
    """``.storage/aoi_sig_mesh.addresses``: provisioner addresses we have sent from, and the
    node addresses we have given out.

    Keyed by Network ID (hex); node blocks under "nodes". Kept when an entry or a device is
    deleted, so an address whose seq values were already spent (in our replay cache, or in a
    node's) is never handed out again.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = _StrictStore(hass, STORAGE_VERSION, f"{DOMAIN}.addresses", atomic_writes=True)
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
            await self._store.async_save_strict(data)

    async def async_used_nodes(self, network_id: str) -> list[tuple[int, int]]:
        data = await self._store.async_load() or {}
        return [(start, count) for start, count in data.get(NODES_KEY, {}).get(network_id, [])]

    async def async_add_nodes(self, network_id: str, blocks: list[tuple[int, int]]) -> None:
        """Record (start, count) blocks that devices on the mesh already hold (imports)."""
        async with self._lock:
            data = await self._store.async_load() or {}
            nodes = data.setdefault(NODES_KEY, {})
            known = {(start, size) for start, size in nodes.get(network_id, [])}
            nodes[network_id] = [list(block) for block in sorted(known | set(blocks))]
            await self._store.async_save_strict(data)

    async def async_reserve_node(
        self,
        network_id: str,
        count: int,
        *,
        taken: list[tuple[int, int]],
        low: int,
        high: int,
    ) -> int:
        """Pick and save the first free block of ``count`` addresses in [low, high].

        Free of ``taken`` (the network's nodes and our address), every node block given out
        before and every provisioner address in this book. Raises MeshError when the range is
        full. Saved before it is returned, so the address is reserved before the device has it.
        """
        async with self._lock:
            data = await self._store.async_load() or {}
            nodes = data.setdefault(NODES_KEY, {})
            reserved = [(start, size) for start, size in nodes.get(network_id, [])]
            used = [(address, 1) for address in data.get(network_id, [])]
            unicast = allocate_unicast([*taken, *reserved, *used], count, low=low, high=high)
            nodes[network_id] = [list(block) for block in sorted([*reserved, (unicast, count)])]
            await self._store.async_save_strict(data)
            return unicast
