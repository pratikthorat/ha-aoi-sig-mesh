import asyncio
import itertools
import logging

import pytest

from mesh import proxy
from mesh.config import (
    OP_COMPOSITION_STATUS,
    config_appkey_add,
    config_composition_get,
    parse_composition_status,
)
from mesh.crypto import k4
from mesh.errors import LinkClosedError, MeshTimeout, SeqNotSaved
from mesh.models import (
    OP_CTL_TEMP_STATUS,
    OP_ONOFF_STATUS,
    ctl_temperature_set,
    onoff_get,
    onoff_set,
)
from mesh.network import BeaconOutcome, MeshNetwork
from mesh.pdu import (
    FILTER_ACCEPT,
    PDU_BEACON,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    NetworkKeys,
    encrypt_network_pdu,
    proxy_pdus,
)
from mesh.proxy import (
    AccessMessage,
    ProxyAdvert,
    ProxyLink,
    node_identity_data,
    node_identity_hash,
    parse_proxy_service_data,
)
from mesh.transport import encrypt_upper, unsegmented_access
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


IDENTITY_RANDOM = bytes.fromhex("34ae608fbbc1f2c6")


def test_node_identity_hash_matches_the_sample_data():
    keys = NetworkKeys.from_net_key(NET_KEY)
    assert keys.identity_key == bytes.fromhex("84396c435ac48560b5965385253e210c")
    assert node_identity_hash(keys.identity_key, IDENTITY_RANDOM, 0x1201) == bytes.fromhex(
        "00861765aefcc57b"
    )


def test_a_node_identity_advert_identifies_only_its_node():
    keys = NetworkKeys.from_net_key(NET_KEY)
    data = node_identity_data(keys.identity_key, IDENTITY_RANDOM, 0x1201)
    assert data == bytes.fromhex("0100861765aefcc57b34ae608fbbc1f2c6")
    advert = parse_proxy_service_data(data)
    assert advert.identifies(keys.identity_key, 0x1201)
    assert not advert.identifies(keys.identity_key, 0x1202)
    other = NetworkKeys.from_net_key(bytes(16))
    assert not advert.identifies(other.identity_key, 0x1201)
    network = parse_proxy_service_data(b"\x00" + keys.network_id)
    assert not network.identifies(keys.identity_key, 0x1201)


def test_node_identity_hash_rejects_a_bad_random():
    with pytest.raises(ValueError):
        node_identity_hash(bytes(16), bytes(7), 0x0001)


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


def test_unsaved_seq_fails_a_send_before_anything_is_written():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        network = MeshNetwork(
            net_key=NET_KEY,
            app_key=APP_KEY,
            address=CLIENT,
            state={"iv_index": 0},
            on_persist=lambda state: None,  # an async save that never confirms
            persist_is_durable=False,
        )
        link = ProxyLink(network, transport, dev_key_for=sim.dev_key_for)
        with pytest.raises(SeqNotSaved):
            await link.send(node.unicast, onoff_get())
        with pytest.raises(SeqNotSaved):  # segmented
            await link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)
        return transport.writes, link.is_connected

    assert run(scenario()) == ([], True)


def notify_onoff_status_from(transport, src, seq=7):
    """A valid OnOff Status to CLIENT, encrypted with the real keys, from any ``src``."""
    access = bytes.fromhex("820401")
    upper = encrypt_upper(
        APP_KEY, akf=1, szmic=0, seq=seq, src=src, dst=CLIENT, iv_index=0, access_payload=access
    )
    lower = unsegmented_access(upper, akf=1, aid=k4(APP_KEY))
    pdu = encrypt_network_pdu(
        NetworkKeys.from_net_key(NET_KEY),
        ctl=0,
        ttl=4,
        seq=seq,
        src=src,
        dst=CLIENT,
        transport_pdu=lower,
        iv_index=0,
    )
    for part in proxy_pdus(PDU_NETWORK, pdu, transport.max_write):
        transport.notify(part)


@pytest.mark.parametrize("src", [0x0000, 0xC000])
def test_pdu_from_a_non_unicast_src_is_dropped(src):
    sim = SimMesh()
    received = []

    async def scenario():
        network, transport, _ = make_link(sim, on_message=received.append)
        notify_onoff_status_from(transport, src)
        notify_onoff_status_from(transport, 0x00B0)  # the same message from a unicast src
        await asyncio.sleep(0.01)
        return network.persistent_state()["replay"]

    replay = run(scenario())
    assert [message.src for message in received] == [0x00B0]
    assert replay == {str(0x00B0): [0, 7]}  # no replay entry for the bad src


def test_unexpected_receive_error_is_logged_and_the_link_stays_up(caplog):
    sim = SimMesh()
    node = sim.add_node("ctl")

    def broken_dev_key_for(address):
        raise RuntimeError("dev key lookup bug")

    async def scenario():
        transport = sim.connect(beacon=False)
        network = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=CLIENT)
        link = ProxyLink(network, transport, dev_key_for=broken_dev_key_for)
        await link.start()
        sim.send_access(node.unicast, CLIENT, bytes.fromhex("820401"), dev_key=node.dev_key)
        await asyncio.sleep(0.01)
        reply = await link.request(node.unicast, onoff_get(), OP_ONOFF_STATUS)
        return link.is_connected, reply.params

    with caplog.at_level(logging.DEBUG, logger="mesh.proxy"):
        assert run(scenario()) == (True, b"\x00")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "dev key lookup bug" in caplog.text


def test_bad_notifications_log_at_debug_only(caplog):
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        _, transport, link = make_link(sim)
        await link.start()
        sim.local_change(node, True)
        await asyncio.sleep(0.01)
        good = sim.last_network
        transport.notify(bytes([PDU_NETWORK]) + good[:-1] + bytes([good[-1] ^ 0xFF]))
        transport.notify(bytes([0x80 | PDU_NETWORK]) + good[:5])  # orphan SAR continuation
        await asyncio.sleep(0.01)

    with caplog.at_level(logging.DEBUG, logger="mesh.proxy"):
        run(scenario())
    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []


class GatedTransport(LoopbackTransport):
    """Holds the second GATT write of a PDU until ``gate`` is set."""

    def __init__(self, mesh):
        super().__init__(mesh)
        self.gate = asyncio.Event()
        self.second_write_started = asyncio.Event()

    async def write(self, data):
        if len(self.writes) == 1:
            self.second_write_started.set()
            await self.gate.wait()
        await super().write(data)


def test_cancelled_send_still_finishes_a_split_pdu():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.transport = GatedTransport(sim)
        _, _, link = make_link(sim, transport)
        sending = asyncio.ensure_future(link.send(node.unicast, onoff_set(True, 1)))
        await transport.second_write_started.wait()  # the PDU needs two 20-byte writes
        sending.cancel()
        await asyncio.sleep(0.01)
        transport.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        return transport.writes

    writes = run(scenario())
    assert len(writes) == 2  # both parts went out
    assert node.on  # the proxy reassembled the whole PDU
