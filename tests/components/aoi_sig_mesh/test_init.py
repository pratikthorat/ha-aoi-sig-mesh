"""Setup, unload, persistence and repairs of a mesh network entry."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aoi_sig_mesh.const import DOMAIN
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.mesh.errors import SeqNotSaved
from custom_components.aoi_sig_mesh.mesh.models import encode_opcode
from custom_components.aoi_sig_mesh.store import NetworkStore
from tests.sim.mesh_sim import CLIENT, SimMesh, SimNode
from tests.sim.sim_radio import BULB_MAC, SimRadio

from .conftest import NETWORK_ID, make_entry, node_data, setup_entry, until


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


async def test_setup_connects_and_reads_every_node(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, radio: SimRadio
) -> None:
    bulb.on, bulb.lightness, bulb.kelvin = True, 0x8000, 3000
    radio.advertise()
    entry = make_entry(("Desk lamp", node_data(bulb)))
    coordinator = await setup_entry(hass, entry)
    record = next(iter(coordinator.nodes.values()))

    await until(lambda: record.state.kelvin == 3000)

    assert entry.state is ConfigEntryState.LOADED
    assert coordinator.connected
    assert radio.connects == [BULB_MAC]
    assert (record.state.on, record.state.lightness) == (True, 0x8000)
    assert sim.filter_addresses == {CLIENT}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_disconnects_and_writes_the_state(
    hass: HomeAssistant, bulb_entry: Any, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    ceiling = coordinator.network.persistent_state()["seq"]

    assert await hass.config_entries.async_unload(bulb_entry.entry_id)

    assert not radio.transports[-1].is_connected
    assert radio.subscribers == 0
    stored = hass_storage[storage_key(bulb_entry.entry_id)]["data"]
    assert stored["seq"] == ceiling
    assert stored["replay"]  # the bulb's statuses were recorded


async def test_the_first_seq_block_is_saved_before_connecting(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    saves: list[tuple[int, int]] = []  # (saved seq ceiling, connections made by then)
    real_save = NetworkStore.async_save

    async def watched(self: NetworkStore, state: dict[str, Any]) -> None:
        saves.append((state["seq"], len(radio.connects)))
        await real_save(self, state)

    radio.advertise()
    entry = make_entry(("Desk lamp", node_data(bulb)))
    with patch.object(NetworkStore, "async_save", watched):
        coordinator = await setup_entry(hass, entry)
        await until(lambda: coordinator.connected and bulb.received)

    assert saves[0] == (64, 0)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_replay_changes_are_saved_in_batches(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, bulb_entry: Any, hass_storage: dict[str, Any]
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    key = storage_key(bulb_entry.entry_id)
    sim.local_change(bulb, True)
    await until(lambda: next(iter(coordinator.nodes.values())).state.on is True)
    live = coordinator.network.persistent_state()["replay"]
    assert hass_storage[key]["data"]["replay"] != live  # not written per message

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()

    assert hass_storage[key]["data"]["replay"] == live


async def test_a_failed_save_is_retried_until_it_lands(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    """Without the retry no seq block is ever reserved again and every send is refused."""
    real_save = NetworkStore.async_save
    failures = [OSError("disk full"), OSError("disk full")]

    async def flaky(self: NetworkStore, state: dict[str, Any]) -> None:
        if failures:
            raise failures.pop()
        await real_save(self, state)

    entry = make_entry(("Desk lamp", node_data(bulb)))
    key = storage_key(entry.entry_id)
    with patch.object(NetworkStore, "async_save", flaky):
        coordinator = await setup_entry(hass, entry)
        assert key not in hass_storage
        with pytest.raises(SeqNotSaved):
            coordinator.network.next_seq()

        for seconds in (1.5, 4.0):  # the retries come 1 s, then 2 s apart
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
            await hass.async_block_till_done()

        assert failures == []
        assert hass_storage[key]["data"]["seq"] == 64
        assert coordinator.network.next_seq() == 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_damaged_node_is_skipped_with_an_issue(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio
) -> None:
    entry = make_entry(("Desk lamp", node_data(bulb)), ("Hall plug", {"unicast": 0x00C0}))

    coordinator = await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert [record.title for record in coordinator.nodes.values()] == ["Desk lamp"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"node_unreadable_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"network": "Home mesh", "names": "Hall plug"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_forgetting_a_node_goes_through_the_link_when_there_is_one(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    link = coordinator.manager.link
    assert str(0x00B0) in coordinator.network.persistent_state()["replay"]

    with patch.object(link, "forget_node", wraps=link.forget_node) as forget:
        coordinator.async_forget_node(0x00B0, 2)

    forget.assert_called_once_with(0x00B0, 2)
    assert str(0x00B0) not in coordinator.network.persistent_state()["replay"]


async def test_forgetting_a_node_without_a_link_clears_the_replay_cache(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    entry = make_entry(("Desk lamp", node_data(bulb)))
    hass_storage[storage_key(entry.entry_id)] = {
        "version": 1,
        "key": storage_key(entry.entry_id),
        "data": {"iv_index": 0, "iv_update": False, "seq": 64, "replay": {"176": [0, 50]}},
    }
    coordinator = await setup_entry(hass, entry)  # nothing advertises: no link

    coordinator.async_forget_node(0x00B0, 2)

    assert coordinator.network.persistent_state()["replay"] == {}
    await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[storage_key(entry.entry_id)]["data"]["replay"] == {}


async def test_replay_entries_of_removed_nodes_are_forgotten(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    """A node removed and provisioned again at its old address starts at seq 0 again."""
    entry = make_entry(("Desk lamp", node_data(bulb)))
    hass_storage[storage_key(entry.entry_id)] = {
        "version": 1,
        "key": storage_key(entry.entry_id),
        "data": {
            "iv_index": 0,
            "iv_update": False,
            "seq": 128,
            "replay": {str(0x00B0): [0, 50], str(0x00C0): [0, 900]},
        },
    }
    await setup_entry(hass, entry)

    stored = hass_storage[storage_key(entry.entry_id)]["data"]  # written by the setup prime
    assert set(stored["replay"]) == {str(0x00B0)}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_imported_iv_index_is_used_until_the_store_exists(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio
) -> None:
    entry = make_entry(("Desk lamp", node_data(bulb)))
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "iv_index": 7})
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data

    assert coordinator.network.iv_index == 7
    await hass.config_entries.async_unload(entry.entry_id)


async def test_node_identity_advert_from_a_known_node_is_used(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio
) -> None:
    radio.advertise(identity=True)
    coordinator = await setup_entry(hass, make_entry(("Desk lamp", node_data(bulb))))

    await until(lambda: coordinator.connected)

    assert radio.connects == [BULB_MAC]
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_wrong_keys_raise_the_keys_rejected_issue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network was reset elsewhere: same Network ID in our entry, new keys on the mesh."""
    other = SimMesh(net_key=bytes(range(16)))
    lamp = other.add_node("ctl", unicast=0x00B0)
    radio = SimRadio(other)
    monkeypatch.setattr("custom_components.aoi_sig_mesh.HaBluetoothRadio", lambda hass: radio)
    radio.advertise(network_id=bytes.fromhex(NETWORK_ID))
    coordinator = await setup_entry(hass, make_entry(("Desk lamp", node_data(lamp))))
    issue_id = f"keys_rejected_{coordinator.entry.entry_id}"

    await until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None)
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_tuya_vendor_messages_are_dropped_quietly(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, bulb_entry: Any
) -> None:
    """Tuya bulbs also talk on their vendor model; those messages change nothing here."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    seen, state = record.last_seen, replace(record.state)

    sim.send_access(bulb.unicast, CLIENT, encode_opcode(0xC907D0) + bytes(4))
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert record.last_seen == seen
    assert record.state == state
    sim.local_change(bulb, True)  # the next real status still gets through
    await until(lambda: record.state.on is True)
