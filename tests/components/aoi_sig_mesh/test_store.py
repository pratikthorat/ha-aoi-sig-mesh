"""NetworkStore, StateSaver (ordered, confirmed writes) and the address book."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.aoi_sig_mesh.mesh.errors import MeshError
from custom_components.aoi_sig_mesh.store import AddressBook, NetworkStore, StateSaver

KEY = "aoi_sig_mesh.entry1"
STATE = {"iv_index": 0, "iv_update": False, "seq": 64, "replay": {}}


def state(seq: int) -> dict[str, Any]:
    return {**STATE, "seq": seq}


async def test_load_returns_what_was_saved(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    await NetworkStore(hass, "entry1").async_save(STATE)

    assert hass_storage[KEY]["data"] == STATE
    assert await NetworkStore(hass, "entry1").async_load() == STATE


async def test_saves_land_in_order_and_each_is_confirmed_after_its_write(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = NetworkStore(hass, "entry1")
    events: list[tuple[str, int]] = []
    real_save = NetworkStore.async_save

    async def slow_save(self: NetworkStore, data: dict[str, Any]) -> None:
        await asyncio.sleep(0.05 if data["seq"] == 64 else 0)  # the first write is slowest
        await real_save(self, data)
        events.append(("written", data["seq"]))

    saver = StateSaver(
        hass, store, lambda data: events.append(("confirmed", data["seq"])), lambda: None
    )
    with patch.object(NetworkStore, "async_save", slow_save):
        saver.save(state(64))
        saver.save(state(128))
        await saver.async_flush()

    assert events == [("written", 64), ("confirmed", 64), ("written", 128), ("confirmed", 128)]
    assert hass_storage[KEY]["data"]["seq"] == 128


async def test_a_failed_disk_write_is_never_confirmed_and_is_reported(
    hass: HomeAssistant, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    confirmed: list[dict[str, Any]] = []
    failures: list[bool] = []

    async def disk_full(self: Store, data: dict[str, Any]) -> None:
        raise WriteError(OSError(28, "No space left on device"))

    saver = StateSaver(
        hass, NetworkStore(hass, "entry1"), confirmed.append, lambda: failures.append(True)
    )
    with patch.object(Store, "_async_write_data", disk_full):
        saver.save(STATE)
        await saver.async_flush()

    assert confirmed == []
    assert failures == [True]
    assert KEY not in hass_storage
    assert "Saving the mesh state failed" in caplog.text


async def test_a_save_while_stopping_is_written_before_it_is_confirmed(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    on_disk_at_confirm: list[bool] = []

    saver = StateSaver(
        hass,
        NetworkStore(hass, "entry1"),
        lambda data: on_disk_at_confirm.append(hass_storage.get(KEY, {}).get("data") == data),
        lambda: None,
    )
    hass.set_state(CoreState.stopping)
    try:
        saver.save(STATE)
        await saver.async_flush()
    finally:
        hass.set_state(CoreState.running)

    assert on_disk_at_confirm == [True]


async def test_a_write_never_runs_inside_save(hass: HomeAssistant) -> None:
    failures: list[bool] = []

    async def broken(self: NetworkStore, data: dict[str, Any]) -> None:
        raise OSError("disk full")

    saver = StateSaver(
        hass, NetworkStore(hass, "entry1"), lambda data: None, lambda: failures.append(True)
    )
    with patch.object(NetworkStore, "async_save", broken):
        saver.save(STATE)
        assert failures == []  # on_failed must not be re-entrant into the caller
        await saver.async_flush()
    assert failures == [True]


async def test_remove_deletes_the_state(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    store = NetworkStore(hass, "entry1")
    await store.async_save(STATE)

    await store.async_remove()

    assert KEY not in hass_storage


async def test_address_book_remembers_addresses_per_network(hass: HomeAssistant) -> None:
    await AddressBook(hass).async_add("aa" * 8, 0x7F00)
    await AddressBook(hass).async_add("aa" * 8, 0x7F00)
    await AddressBook(hass).async_add("aa" * 8, 0x7F01)

    assert await AddressBook(hass).async_used("aa" * 8) == [(0x7F00, 1), (0x7F01, 1)]
    assert await AddressBook(hass).async_used("bb" * 8) == []


async def test_concurrent_address_adds_are_not_lost(hass: HomeAssistant) -> None:
    await asyncio.gather(
        AddressBook(hass).async_add("aa" * 8, 0x7F00),
        AddressBook(hass).async_add("aa" * 8, 0x7F01),
        AddressBook(hass).async_add("aa" * 8, 0x7F02),
    )

    assert await AddressBook(hass).async_used("aa" * 8) == [(0x7F00, 1), (0x7F01, 1), (0x7F02, 1)]


async def test_node_reservations_skip_taken_reserved_and_provisioner_addresses(
    hass: HomeAssistant,
) -> None:
    book = AddressBook(hass)
    await book.async_add("aa" * 8, 0x0003)  # an address we once sent from

    first = await book.async_reserve_node("aa" * 8, 2, taken=[(0x0001, 1)], low=2, high=0x7EFF)
    second = await book.async_reserve_node("aa" * 8, 1, taken=[(0x0001, 1)], low=2, high=0x7EFF)

    # 0x0002 alone is too small for two elements, but takes the next one-element node
    assert (first, second) == (0x0004, 0x0002)
    assert await AddressBook(hass).async_used_nodes("aa" * 8) == [(0x0002, 1), (0x0004, 2)]
    assert await AddressBook(hass).async_used_nodes("bb" * 8) == []
    assert await AddressBook(hass).async_used("aa" * 8) == [(0x0003, 1)]


async def test_concurrent_node_reservations_never_collide(hass: HomeAssistant) -> None:
    results = await asyncio.gather(
        *(
            AddressBook(hass).async_reserve_node("aa" * 8, 1, taken=[], low=2, high=9)
            for _ in range(4)
        )
    )

    assert sorted(results) == [2, 3, 4, 5]


async def test_a_full_range_reserves_nothing(hass: HomeAssistant) -> None:
    book = AddressBook(hass)
    with pytest.raises(MeshError):
        await book.async_reserve_node("aa" * 8, 2, taken=[(2, 1)], low=2, high=3)

    assert await book.async_used_nodes("aa" * 8) == []
