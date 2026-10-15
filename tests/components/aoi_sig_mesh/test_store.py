"""NetworkStore, StateSaver (ordered, confirmed writes) and the address book."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

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


async def test_a_failed_write_is_never_confirmed_and_is_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    confirmed: list[dict[str, Any]] = []
    failures: list[bool] = []

    async def broken(self: NetworkStore, data: dict[str, Any]) -> None:
        raise OSError("disk full")

    saver = StateSaver(
        hass, NetworkStore(hass, "entry1"), confirmed.append, lambda: failures.append(True)
    )
    with patch.object(NetworkStore, "async_save", broken):
        saver.save(STATE)
        await saver.async_flush()

    assert confirmed == []
    assert failures == [True]
    assert "Saving the mesh state failed" in caplog.text


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
