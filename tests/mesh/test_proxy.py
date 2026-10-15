import asyncio
import itertools
import logging

import pytest

from mesh import proxy
from mesh.config import OP_COMPOSITION_STATUS, config_composition_get, parse_composition_status
from mesh.errors import LinkClosedError, MeshTimeout
from mesh.models import (
    OP_CTL_TEMP_STATUS,
    OP_ONOFF_STATUS,
    ctl_temperature_set,
    onoff_get,
    onoff_set,
)
from mesh.network import BeaconOutcome, MeshNetwork
from mesh.pdu import FILTER_ACCEPT, PDU_BEACON, PDU_NETWORK, PDU_PROXY_CONFIG, NetworkKeys
from mesh.proxy import AccessMessage, ProxyAdvert, ProxyLink, parse_proxy_service_data
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, LoopbackTransport, SimMesh, run


def make_link(sim, transport=None, **kwargs):
    """A ProxyLink from CLIENT to the simulated mesh. Call it inside the running loop."""
    if transport is None:
        transport = sim.connect(beacon=False)
    network = MeshNetwork(
        net_key=NET_KEY,
        app_key=APP_KEY,
        address=CLIENT,
        state={"iv_index": sim.iv_index, "iv_update": sim.iv_update},
    )
    link = ProxyLink(network, transport, dev_key_for=sim.dev_key_for, **kwargs)
    return network, transport, link


def test_start_sets_the_proxy_filter():
    sim = SimMesh()

    async def scenario():
        _, _, link = make_link(sim)
        await link.start()
        return link.filter_ok

    assert run(scenario()) is True
    assert (sim.filter_type, sim.filter_addresses) == (FILTER_ACCEPT, {CLIENT})


def test_start_carries_on_when_the_proxy_ignores_the_filter():
    sim = SimMesh()
    node = sim.add_node("ctl")
    sim.ignore_proxy_config = True

    async def scenario():
        _, _, link = make_link(sim)
        await link.start(filter_timeout=0.05)
        reply = await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)
        return link.filter_ok, reply

    filter_ok, reply = run(scenario())
    assert filter_ok is False
    assert reply.params == b"\x00"  # the proxy adds our src to its filter by itself


def test_onoff_round_trip():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        _, _, link = make_link(sim)
        await link.start()
        return await link.request(node.unicast, onoff_set(True, 1), OP_ONOFF_STATUS)

    assert run(scenario()) == AccessMessage(0x00B0, CLIENT, OP_ONOFF_STATUS, b"\x01", dev_key=False)
    assert node.on


def test_ctl_temperature_goes_to_the_second_element():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        _, _, link = make_link(sim)
        return await link.request(0x00B1, ctl_temperature_set(3000, 1), OP_CTL_TEMP_STATUS)

    reply = run(scenario())
    assert (reply.src, reply.params) == (0x00B1, bytes.fromhex("b80b0000"))
    assert node.kelvin == 3000


def test_request_times_out_after_three_attempts(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.05)
    sim = SimMesh()
    node = sim.add_node("ctl")
    sim.silent = True

    async def scenario():
        _, _, link = make_link(sim)
        with pytest.raises(MeshTimeout):
            await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)

    run(scenario())
    assert len(node.received) == 3
    assert len({pdu.seq for pdu in sim.from_client}) == 3  # every attempt has a new seq


def test_request_retries_after_a_lost_message(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.05)
    sim = SimMesh()
    node = sim.add_node("ctl")
    sim.drop_from_client = 1

    async def scenario():
        _, _, link = make_link(sim)
        return await link.request(node.unicast, onoff_set(True, 7), OP_ONOFF_STATUS)

    assert run(scenario()).params == b"\x01"
    assert len(node.received) == 1


def test_two_identical_requests_both_complete():
    sim = SimMesh()
    node = sim.add_node("ctl")
    sim.drop_from_client = 1  # the first request's Get never arrives

    async def scenario():
        _, _, link = make_link(sim)
        return await asyncio.gather(
            link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS),
            link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS),
        )

    first, second = run(scenario(), timeout=1.0)  # well under REQUEST_TIMEOUT: no retry happened
    assert first == second
    assert len(node.received) == 1  # one Get reached the node and its one reply answered both


def test_unsolicited_status_reaches_on_message():
    sim = SimMesh()
    node = sim.add_node("ctl")
    received = []

    async def scenario():
        _, _, link = make_link(sim, on_message=received.append)
        await link.start()
        sim.local_change(node, True)
        await asyncio.sleep(0.01)

    run(scenario())
    assert received == [AccessMessage(0x00B0, CLIENT, OP_ONOFF_STATUS, b"\x01", dev_key=False)]


def test_replayed_message_is_dropped():
    sim = SimMesh()
    node = sim.add_node("ctl")
    received = []

    async def scenario():
        _, _, link = make_link(sim, on_message=received.append)
        await link.start()
        sim.local_change(node, True)
        await asyncio.sleep(0.01)
        sim.replay_last_to_client()
        await asyncio.sleep(0.01)

    run(scenario())
    assert len(received) == 1


def test_junk_notifications_are_ignored():
    sim = SimMesh()
    node = sim.add_node("ctl")
    received = []

    async def scenario():
        _, transport, link = make_link(sim, on_message=received.append)
        await link.start()
        sim.local_change(node, True)
        await asyncio.sleep(0.01)
        good = sim.last_network
        bad_mic = good[:-1] + bytes([good[-1] ^ 0xFF])
        junk = [
            b"",
            bytes([PDU_NETWORK]),  # empty network PDU
            bytes([PDU_NETWORK]) + good[:12],  # truncated
            bytes([PDU_NETWORK]) + bad_mic,
            bytes([0x80 | PDU_NETWORK]) + good[:5],  # orphan SAR continuation
            bytes([0xC0 | PDU_NETWORK]) + good[:5],  # orphan SAR last
            bytes([PDU_BEACON]) + bytes(22),
            bytes([PDU_PROXY_CONFIG]) + bytes(20),
            bytes([0x3F, 0x01, 0x02]),  # unknown proxy PDU type
        ]
        for data in junk:
            transport.notify(data)
        # A status encrypted with a DevKey we do not have for that node.
        sim.send_access(node.unicast, CLIENT, bytes.fromhex("820401"), dev_key=bytes(16))
        await asyncio.sleep(0.01)
        return await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)

    reply = run(scenario())
    assert reply.params == b"\x01"
    assert [message.params for message in received] == [b"\x01", b"\x01"]


def test_composition_via_dev_key_is_reassembled_and_acked():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        _, _, link = make_link(sim)
        await link.start()
        reply = await link.request(
            node.unicast, config_composition_get(), OP_COMPOSITION_STATUS, dev_key=node.dev_key
        )
        await asyncio.sleep(0.01)  # let the Segment Ack go out
        return reply

    reply = run(scenario())
    assert reply.dev_key is True
    assert parse_composition_status(reply.params) == node.composition()
    [ack] = sim.acks_from_client
    assert ack.block_ack == 0b1111  # 36-byte access + 4-byte TransMIC = 4 segments


def test_forget_node_lets_a_reset_node_start_again_from_seq_0():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        _, _, link = make_link(sim)
        await link.start()
        get = config_composition_get()
        await link.request(node.unicast, get, OP_COMPOSITION_STATUS, dev_key=node.dev_key)
        sim._seq = itertools.count()  # the node was reset and re-provisioned: its SEQ restarts
        link.forget_node(node.unicast, 2)
        return await link.request(node.unicast, get, OP_COMPOSITION_STATUS, dev_key=node.dev_key)

    reply = run(scenario())
    assert parse_composition_status(reply.params) == node.composition()


class BrokenTransport(LoopbackTransport):
    async def write(self, data):
        raise OSError("GATT write failed")


def test_link_drop_fails_pending_request_and_calls_on_close_once():
    sim = SimMesh()
    node = sim.add_node("ctl")
    sim.silent = True
    closed = []

    async def scenario():
        _, transport, link = make_link(sim, on_close=lambda: closed.append(True))
        pending = asyncio.ensure_future(link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS))
        await asyncio.sleep(0.01)
        transport.drop()
        with pytest.raises(LinkClosedError):
            await asyncio.wait_for(pending, 0.5)  # promptly, not after REQUEST_TIMEOUT
        with pytest.raises(LinkClosedError):
            await link.send(node.unicast, onoff_get())
        await link.close()  # closing a dropped link is harmless
        return link.is_connected

    assert run(scenario()) is False
    assert closed == [True]


def test_close_does_not_call_on_close():
    sim = SimMesh()
    node = sim.add_node("ctl")
    closed = []

    async def scenario():
        _, transport, link = make_link(sim, on_close=lambda: closed.append(True))
        await link.close()
        await asyncio.sleep(0.01)  # the transport's disconnect callback runs here
        with pytest.raises(LinkClosedError):
            await link.send(node.unicast, onoff_get())
        return transport.is_connected

    assert run(scenario()) is False
    assert closed == []


def test_failed_write_closes_the_link():
    sim = SimMesh()
    node = sim.add_node("ctl")
    closed = []

    async def scenario():
        _, _, link = make_link(sim, BrokenTransport(sim), on_close=lambda: closed.append(True))
        with pytest.raises(LinkClosedError, match="GATT write failed"):
            await link.send(node.unicast, onoff_get())
        return link.is_connected

    assert run(scenario()) is False
    assert closed == [True]


def test_iv_update_beacons_are_applied_and_the_link_keeps_working():
    sim = SimMesh()
    node = sim.add_node("ctl")
    outcomes = []

    async def scenario():
        network, _, link = make_link(
            sim, on_beacon=lambda beacon, outcome: outcomes.append(outcome)
        )
        await link.start()
        sim.iv_index, sim.iv_update = 1, True
        sim.send_beacon()
        await asyncio.sleep(0.01)
        during = (network.iv_index, network.iv_update, network.tx_iv_index)
        first = await link.request(node.unicast, onoff_set(True, 1), OP_ONOFF_STATUS)
        sim.iv_update = False
        sim.reset_seq()  # nodes restart their seq when the update completes
        sim.send_beacon()
        await asyncio.sleep(0.01)
        after = (network.iv_index, network.iv_update, network.tx_iv_index)
        second = await link.request(node.unicast, onoff_set(False, 2), OP_ONOFF_STATUS)
        return during, after, first.params, second.params

    during, after, first, second = run(scenario())
    assert during == (1, True, 0)  # still sending with the old IV index
    assert after == (1, False, 1)
    assert (first, second) == (b"\x01", b"\x00")
    assert outcomes == [BeaconOutcome.UPDATED, BeaconOutcome.UPDATED]


def test_beacon_from_another_network_is_ignored():
    sim = SimMesh()
    outcomes = []

    async def scenario():
        network, _, _ = make_link(sim, on_beacon=lambda beacon, outcome: outcomes.append(outcome))
        sim.send_beacon(iv_index=1, net_key=bytes(range(16)))
        await asyncio.sleep(0.01)
        return network.iv_index

    assert run(scenario()) == 0
    assert outcomes == []


def test_beacon_too_far_ahead_is_reported_not_applied():
    sim = SimMesh()
    outcomes = []

    async def scenario():
        network, _, _ = make_link(sim, on_beacon=lambda beacon, outcome: outcomes.append(outcome))
        sim.send_beacon(iv_index=5)
        await asyncio.sleep(0.01)
        return network.iv_index

    assert run(scenario()) == 0
    assert outcomes == [BeaconOutcome.TOO_FAR]


def test_parse_proxy_service_data():
    network_id = NetworkKeys.from_net_key(NET_KEY).network_id
    assert parse_proxy_service_data(b"\x00" + network_id) == ProxyAdvert(network_id=network_id)
    identity = bytes(range(1, 17))
    assert parse_proxy_service_data(b"\x01" + identity) == ProxyAdvert(
        node_hash=identity[:8], node_random=identity[8:]
    )
    assert parse_proxy_service_data(b"") is None
    assert parse_proxy_service_data(b"\x00" + network_id[:7]) is None
    assert parse_proxy_service_data(b"\x02" + network_id) is None
    assert parse_proxy_service_data(b"\x01" + identity[:15]) is None
    assert parse_proxy_service_data(b"\x01" + identity + b"\x00") is None


def test_too_far_beacon_warns_once_per_iv_index(caplog):
    sim = SimMesh()
    outcomes = []

    async def scenario():
        make_link(sim, on_beacon=lambda beacon, outcome: outcomes.append(outcome))
        for iv_index in (5, 5, 5, 6):
            sim.send_beacon(iv_index=iv_index)
            await asyncio.sleep(0.01)

    with caplog.at_level(logging.DEBUG, logger="mesh.proxy"):
        run(scenario())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "IV index 5" in warnings[0] and "IV index 6" in warnings[1]
    assert outcomes == [BeaconOutcome.TOO_FAR] * 4


def test_beacon_that_cannot_be_saved_is_logged_and_the_link_stays_up(caplog):
    sim = SimMesh()
    node = sim.add_node("ctl")
    outcomes = []

    def failing_apply_beacon(beacon):
        raise OSError("disk full")

    async def scenario():
        network, _, link = make_link(
            sim, on_beacon=lambda beacon, outcome: outcomes.append(outcome)
        )
        network.apply_beacon = failing_apply_beacon
        sim.send_beacon(iv_index=1)
        await asyncio.sleep(0.01)
        reply = await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)
        return link.is_connected, reply.params

    assert run(scenario()) == (True, b"\x00")
    assert "Could not save the IV index from a beacon" in caplog.text
    assert outcomes == []


def test_failing_on_beacon_callback_leaves_the_link_up(caplog):
    sim = SimMesh()
    node = sim.add_node("ctl")

    def broken_callback(beacon, outcome):
        raise RuntimeError("callback bug")

    async def scenario():
        network, _, link = make_link(sim, on_beacon=broken_callback)
        sim.iv_index = 1
        sim.send_beacon()
        await asyncio.sleep(0.01)
        reply = await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)
        return link.is_connected, network.iv_index, reply.params

    assert run(scenario()) == (True, 1, b"\x00")
    assert "on_beacon callback failed" in caplog.text
