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

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.const import CONF_CONFIGURED, CONF_MAC, CONF_ROLE, DOMAIN
from custom_components.aoi_sig_mesh.coordinator import (
    MeshCoordinator,
    claimed_range,
    read_node,
)
from custom_components.aoi_sig_mesh.mesh.config import OP_MODEL_APP_BIND
from custom_components.aoi_sig_mesh.mesh.configure import ConfigureError
from custom_components.aoi_sig_mesh.mesh.errors import LinkClosedError, SeqNotSaved
from custom_components.aoi_sig_mesh.mesh.models import encode_opcode, parse_opcode
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


async def test_a_setup_cut_short_by_a_drop_is_retried_on_the_next_connect(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, caplog: pytest.LogCaptureFixture
) -> None:
    """A drop during the binds must not save the node as configured with nothing bound."""
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    real_handle = lamp.handle
    dropped: list[int] = []

    def handle(dst: int, access: bytes, *, dev_key: bool) -> list[tuple[int, bytes]]:
        if not dropped and parse_opcode(access)[0] == OP_MODEL_APP_BIND:
            dropped.append(dst)  # the proxy goes away at the first Model App Bind
            radio.forget()
            radio.transports[-1].drop()
            return []
        return real_handle(dst, access, dev_key=dev_key)

    radio.advertise()
    # capabilities as Composition reports them: the setup then records no change, no reload
    entry = make_entry(("Desk lamp", {**node_data(lamp), CONF_CONFIGURED: False}))
    with patch.object(lamp, "handle", handle):
        coordinator = await setup_entry(hass, entry)
        record = next(iter(coordinator.nodes.values()))
        await until(lambda: "retrying next connect" in caplog.text)

        assert dropped
        assert record.configured is False
        assert entry.subentries[record.subentry_id].data[CONF_CONFIGURED] is False
        assert not lamp.bindings

        radio.advertise()
        await until(lambda: record.configured)

    assert entry.subentries[record.subentry_id].data[CONF_CONFIGURED] is True
    assert (0x00B0, 0x1000) in lamp.bindings
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_healthy_network_raises_no_keys_issue(hass: HomeAssistant, bulb_entry: Any) -> None:
    issue_id = f"keys_rejected_{bulb_entry.entry_id}"

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_beacon_alone_proves_the_keys(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, radio: SimRadio
) -> None:
    """Proxies send a beacon as soon as the link is up: it counts, even if no node answers."""
    sim.silent = True
    radio.advertise()
    coordinator = await setup_entry(hass, make_entry(("Desk lamp", node_data(bulb))))
    record = next(iter(coordinator.nodes.values()))

    await until(lambda: record.failures >= 1)  # the first round of reads is over

    issue_id = f"keys_rejected_{coordinator.entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_the_keys_issue_clears_once_the_mesh_is_heard(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, radio: SimRadio
) -> None:
    sim.silent = True
    radio.advertise()
    registry = ir.async_get(hass)
    with patch.object(sim, "connect", lambda: SimMesh.connect(sim, beacon=False)):
        coordinator = await setup_entry(hass, make_entry(("Desk lamp", node_data(bulb))))
        issue_id = f"keys_rejected_{coordinator.entry.entry_id}"
        await until(lambda: registry.async_get_issue(DOMAIN, issue_id) is not None)

        sim.silent = False
        sim.local_change(bulb, True)

        await until(lambda: registry.async_get_issue(DOMAIN, issue_id) is None)
    await hass.config_entries.async_unload(coordinator.entry.entry_id)


async def test_a_repeated_far_beacon_warns_once_per_iv_index(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Proxies repeat their beacon every few seconds; the log must not fill up with it."""

    def warnings() -> int:
        return sum(
            record.name == coordinator_module.__name__
            and record.levelname == "WARNING"
            and "more than one step" in record.getMessage()
            for record in caplog.records
        )

    coordinator: MeshCoordinator = bulb_entry.runtime_data
    sim.send_beacon(iv_index=5)
    sim.send_beacon(iv_index=5)
    await until(lambda: coordinator.pending_iv_jump is not None)
    await hass.async_block_till_done()
    assert warnings() == 1

    sim.send_beacon(iv_index=6)
    await until(lambda: coordinator.pending_iv_jump.iv_index == 6)
    assert warnings() == 2


async def test_an_overlapping_shutdown_waits_for_the_first(
    hass: HomeAssistant, bulb_entry: Any, hass_storage: dict[str, Any]
) -> None:
    """HA stopping while the entry unloads: neither caller may return before the state is saved."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    real_save = NetworkStore.async_save
    reached = asyncio.Event()
    release = asyncio.Event()

    async def held(self: NetworkStore, state: dict[str, Any]) -> None:
        reached.set()
        await release.wait()
        await real_save(self, state)

    with patch.object(NetworkStore, "async_save", held):
        first = hass.async_create_task(coordinator.async_shutdown())
        second = hass.async_create_task(coordinator.async_shutdown())
        await until(reached.is_set)

        assert not first.done()
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)

    stored = hass_storage[storage_key(bulb_entry.entry_id)]["data"]
    assert stored == coordinator.network.persistent_state()


@pytest.mark.parametrize("damage", [{CONF_MAC: 123}, {CONF_ROLE: 7}, {CONF_ROLE: "fan"}])
async def test_a_bad_mac_or_role_skips_the_node_with_an_issue(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, damage: dict[str, Any]
) -> None:
    plug = {**node_data(bulb), "unicast": 0x00C0, **damage}
    entry = make_entry(("Desk lamp", node_data(bulb)), ("Hall plug", plug))

    coordinator = await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert [record.title for record in coordinator.nodes.values()] == ["Desk lamp"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"node_unreadable_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["names"] == "Hall plug"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_explicit_role_of_none_is_treated_as_missing(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio
) -> None:
    entry = make_entry(("Desk lamp", {**node_data(bulb), CONF_ROLE: None}))

    coordinator = await setup_entry(hass, entry)

    record = next(iter(coordinator.nodes.values()))
    assert record.role == "light"
    assert not coordinator.needs_reload()
    assert hass.states.get("light.desk_lamp") is not None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "damage",
    [
        {"element_count": float("inf")},
        {"kelvin_min": float("-inf")},
        {"models": {"onoff": float("inf")}},
    ],
)
async def test_an_infinite_number_skips_the_node_with_an_issue(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, damage: dict[str, Any]
) -> None:
    plug = {**node_data(bulb), "unicast": 0x00C0, **damage}
    entry = make_entry(("Desk lamp", node_data(bulb)), ("Hall plug", plug))

    coordinator = await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert [record.title for record in coordinator.nodes.values()] == ["Desk lamp"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"node_unreadable_{entry.entry_id}")
    assert issue is not None
    await hass.config_entries.async_unload(entry.entry_id)


def test_claimed_range_reads_past_an_infinite_number(bulb: SimNode) -> None:
    assert read_node({**node_data(bulb), "unicast": float("inf")}) is None
    assert claimed_range({"unicast": float("inf"), "element_count": 1}) is None
    assert claimed_range({"unicast": 0x00C0, "element_count": float("inf")}) == (0x00C0, 1)


async def test_replay_entries_of_a_damaged_node_are_kept(
    hass: HomeAssistant, bulb: SimNode, radio: SimRadio, hass_storage: dict[str, Any]
) -> None:
    """The node is still on the mesh; forgetting its seq would let old messages replay."""
    entry = make_entry(
        ("Desk lamp", node_data(bulb)),
        ("Hall plug", {"unicast": 0x00C0, "element_count": 2}),  # no DevKey: damaged
    )
    key = storage_key(entry.entry_id)
    replay = {str(address): [0, 50] for address in (0x00B0, 0x00C0, 0x00C1, 0x00D0)}
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"iv_index": 0, "iv_update": False, "seq": 128, "replay": replay},
    }
    await setup_entry(hass, entry)

    stored = hass_storage[key]["data"]
    assert set(stored["replay"]) == {str(0x00B0), str(0x00C0), str(0x00C1)}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_after_a_failed_save_lands_the_connect_pass_runs_again(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """Setup and reads skipped while the save failed must not wait for the next drop."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    record.configured = False  # as if its setup had been skipped with SeqNotSaved
    real_save = NetworkStore.async_save
    failures = [OSError("disk full")]

    async def flaky(self: NetworkStore, state: dict[str, Any]) -> None:
        if failures:
            raise failures.pop()
        await real_save(self, state)

    with patch.object(NetworkStore, "async_save", flaky):
        coordinator.network.prime()
        await hass.async_block_till_done()
        assert failures == []
        received = len(bulb.received)
        assert not record.configured

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.5))  # the retry
        await until(lambda: record.configured and len(bulb.received) > received)


async def test_the_idle_timer_restarts_after_each_setup_and_read(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    """Only get_link() counts as activity, so a long setup or read must touch the manager."""
    coordinator: MeshCoordinator = bulb_entry.runtime_data
    record = next(iter(coordinator.nodes.values()))
    record.configured = False
    events: list[str] = []

    async def slow_configure(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0.1)
        events.append("configure done")
        raise ConfigureError("composition", "no answer")

    async def slow_refresh(_self: Any) -> int:
        await asyncio.sleep(0.1)
        events.append("refresh done")
        return 1

    with (
        patch.object(coordinator.manager, "touch", lambda: events.append("touch")),
        patch.object(coordinator_module, "configure_node", slow_configure),
        patch.object(coordinator_module.NodeCommands, "refresh", slow_refresh),
    ):
        radio.transports[-1].drop()  # reconnect: setup, then reads
        await until(lambda: "refresh done" in events and events[-1] == "touch")

    start = events.index("configure done")
    assert events[start:] == ["configure done", "touch", "refresh done", "touch"]


async def test_tids_continue_across_a_reload(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """A reload inside 6 s of a command must not hand the bulb that command's TID again."""

    async def last_tid(service: str) -> int:
        await hass.services.async_call(
            "light", service, {"entity_id": "light.desk_lamp"}, blocking=True
        )
        return bulb.tids[bulb.unicast][0]

    first = await last_tid("turn_on")
    assert await hass.config_entries.async_reload(bulb_entry.entry_id)
    await until(lambda: bulb_entry.runtime_data.connected)

    assert await last_tid("turn_off") == (first + 1) & 0xFF

    await hass.config_entries.async_remove(bulb_entry.entry_id)
    assert bulb_entry.entry_id not in hass.data.get(DOMAIN, {})  # removal drops the counters


async def test_an_unloaded_network_sends_nothing(hass: HomeAssistant, bulb_entry: Any) -> None:
    coordinator: MeshCoordinator = bulb_entry.runtime_data

    await hass.config_entries.async_unload(bulb_entry.entry_id)

    with pytest.raises(LinkClosedError):
        coordinator.network.next_seq()
