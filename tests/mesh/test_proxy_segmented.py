import asyncio
import itertools

import pytest

from mesh import proxy
from mesh.config import OP_APPKEY_STATUS, STATUS_SUCCESS, config_appkey_add
from mesh.errors import LinkClosedError, MeshError, MeshTimeout
from mesh.pdu import (
    PDU_NETWORK,
    NetworkKeys,
    ProxyReassembler,
    decrypt_network_pdu,
    encrypt_network_pdu,
    proxy_pdus,
)
from mesh.transport import make_segment_ack, parse_lower_access
from tests.mesh.test_proxy import make_link
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, run

NODE = 0x00B0
SIX_SEGMENTS = bytes(range(60))  # a 60-byte access message + 4-byte TransMIC = 6 segments


def unconfigured_node():
    sim = SimMesh()
    return sim, sim.add_node("ctl", configured=False)


def test_appkey_add_is_sent_segmented():
    sim, node = unconfigured_node()

    async def scenario():
        _, _, link = make_link(sim)
        return await link.request(
            node.unicast, config_appkey_add(0, 0, APP_KEY), OP_APPKEY_STATUS, dev_key=node.dev_key
        )

    reply = run(scenario())
    assert reply.params[0] == STATUS_SUCCESS
    assert node.has_app_key
    assert len(sim.from_client) == 2  # 20-byte access + 4-byte TransMIC = 2 segments


def test_segmented_send_resends_lost_segments(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.05)
    sim, node = unconfigured_node()
    sim.drop_from_client = 1  # segment 0 is lost

    async def scenario():
        _, _, link = make_link(sim)
        await link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)

    run(scenario())
    assert node.has_app_key
    seqs = [pdu.seq for pdu in sim.from_client]
    # Segment 1, then segment 0 with a new seq. That completes the message, and its ack
    # lands before segment 1 would be sent again.
    assert seqs == [1, 2]


def test_a_resent_segment_other_than_segment_0_gets_a_fresh_seq(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.05)
    sim, node = unconfigured_node()
    deliver = sim._on_network
    arrived = []

    def lose_the_first_segment_1(payload):
        arrived.append(payload)
        if len(arrived) != 2:  # the second network PDU is segment 1's first send
            deliver(payload)

    monkeypatch.setattr(sim, "_on_network", lose_the_first_segment_1)

    async def scenario():
        _, _, link = make_link(sim)
        await link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)

    run(scenario())
    assert node.has_app_key
    sent = [(parse_lower_access(pdu.transport_pdu).seg_o, pdu.seq) for pdu in sim.from_client]
    # Segment 0 went out as SeqAuth 0, segment 1 as seq 1 and was lost. No ack came in time,
    # so the next round resent both, each with a fresh seq: reusing 1 would be a replay.
    assert sent == [(0, 0), (0, 2), (1, 3)]


def test_segmented_send_times_out_without_acks(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.02)
    sim, node = unconfigured_node()
    sim.silent = True

    async def scenario():
        _, _, link = make_link(sim)
        with pytest.raises(MeshTimeout):
            await link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)

    run(scenario())
    assert len(sim.from_client) == 2 * (1 + proxy.SEGMENT_RETRANSMITS)


def test_request_keeps_a_response_whose_segment_acks_were_lost(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.02)
    sim, node = unconfigured_node()
    sim.drop_acks_to_client = True  # the node gets the message and answers, its acks are lost

    async def scenario():
        _, _, link = make_link(sim)
        return await link.request(
            node.unicast, config_appkey_add(0, 0, APP_KEY), OP_APPKEY_STATUS, dev_key=node.dev_key
        )

    reply = run(scenario())
    assert reply.params[0] == STATUS_SUCCESS  # the status proves delivery: no MeshTimeout
    assert node.has_app_key
    assert len(sim.from_client) == 2 * (1 + proxy.SEGMENT_RETRANSMITS)  # no ack ever came


def test_rejected_segmented_send_raises():
    sim, node = unconfigured_node()
    sim.reject_segments = True

    async def scenario():
        _, _, link = make_link(sim)
        with pytest.raises(MeshError, match="rejected") as caught:
            await link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)
        return caught.value

    assert not isinstance(run(scenario()), MeshTimeout)
    assert not node.has_app_key


def test_link_drop_during_segmented_send_raises_promptly():
    sim, node = unconfigured_node()
    sim.silent = True

    async def scenario():
        _, transport, link = make_link(sim)
        sending = asyncio.ensure_future(
            link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)
        )
        await asyncio.sleep(0.01)
        transport.drop()
        with pytest.raises(LinkClosedError):
            await asyncio.wait_for(sending, 0.2)  # well under SEGMENT_ACK_TIMEOUT (1 s)
        return link

    assert run(scenario())._outgoing is None


def record_network_pdus(monkeypatch):
    """Record the (IV index, seq) of every network PDU the link encrypts."""
    sent = []
    encrypt = proxy.encrypt_network_pdu

    def recording(keys, **kwargs):
        sent.append((kwargs["iv_index"], kwargs["seq"]))
        return encrypt(keys, **kwargs)

    monkeypatch.setattr(proxy, "encrypt_network_pdu", recording)
    return sent


def test_iv_update_completing_mid_send_never_reuses_a_seq(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.05)
    sent = record_network_pdus(monkeypatch)
    sim = SimMesh(iv_index=1, iv_update=True)
    node = sim.add_node("ctl", configured=False)
    sim.silent = True

    async def scenario():
        network, _, link = make_link(sim)
        sending = asyncio.ensure_future(
            link.send(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=node.dev_key)
        )
        await asyncio.sleep(0.01)  # the first round is out, waiting for acks
        sim.iv_update = False
        sim.send_beacon()  # the IV Update completes: seq restarts at 0 under IV index 1
        await asyncio.sleep(0.01)
        assert network.tx_iv_index == 1
        with pytest.raises(MeshError) as caught:
            await sending
        return caught.value, link

    error, link = run(scenario())
    assert sent[:2] == [(0, 0), (0, 1)]  # the first round, before the beacon
    assert len(sent) == len(set(sent)), sent  # no (IV index, seq) pair is used twice
    assert not isinstance(error, MeshTimeout)
    assert link._outgoing is None


class AckingProxy:
    """A stub GATT link whose node answers each segment it receives with ``respond``.

    ``respond(seg_o, seq_zero, acked)`` returns the (src, seq_zero, block_ack) Segment Acks
    to send back; ``acked`` is the node's own record, which ``respond`` may update.
    """

    max_write = 100  # one proxy PDU per network PDU

    def __init__(self, respond):
        self.is_connected = True
        self.segments: list[int] = []  # seg_o of every segment the client wrote
        self.acked: set[int] = set()
        self._respond = respond
        self._keys = NetworkKeys.from_net_key(NET_KEY)
        self._proxy_rx = ProxyReassembler()
        self._seq = itertools.count()
        self._notify = None

    def set_notify_callback(self, callback):
        self._notify = callback

    def set_disconnect_callback(self, callback):
        pass

    async def write(self, data):
        pdu_type, payload = self._proxy_rx.feed(data)
        net = decrypt_network_pdu(self._keys, payload, 0)
        if pdu_type != PDU_NETWORK or net is None or net.ctl:
            return
        lower = parse_lower_access(net.transport_pdu)
        self.segments.append(lower.seg_o)
        for src, seq_zero, block_ack in self._respond(lower.seg_o, lower.seq_zero, self.acked):
            ack = encrypt_network_pdu(
                self._keys,
                ctl=1,
                ttl=4,
                seq=next(self._seq),
                src=src,
                dst=CLIENT,
                transport_pdu=make_segment_ack(seq_zero, block_ack),
                iv_index=0,
            )
            for part in proxy_pdus(PDU_NETWORK, ack, self.max_write):
                asyncio.get_running_loop().call_soon(self._notify, part)

    async def close(self):
        self.is_connected = False


def block(acked):
    return sum(1 << seg_o for seg_o in acked)


def send_through(stub):
    async def scenario():
        _, _, link = make_link(SimMesh(), stub)
        await link.send(NODE, SIX_SEGMENTS)

    run(scenario())


def test_a_send_that_keeps_making_progress_completes(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.05)

    def one_new_segment_per_round(seg_o, seq_zero, acked):
        if seg_o != max(set(range(6)) - acked):
            return []  # only the highest missing segment, the round's last, gets through
        acked.add(seg_o)
        return [(NODE, seq_zero, block(acked))]

    stub = AckingProxy(one_new_segment_per_round)
    send_through(stub)  # six rounds, more than 1 + SEGMENT_RETRANSMITS, but each one progressed
    assert stub.acked == set(range(6))
    assert stub.segments == [*range(6), *range(5), *range(4), *range(3), *range(2), 0]


def test_a_repeated_partial_ack_does_not_count_as_progress(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.05)

    def always_only_segment_0(seg_o, seq_zero, acked):
        return [(NODE, seq_zero, 0b1)]

    stub = AckingProxy(always_only_segment_0)
    with pytest.raises(MeshTimeout):
        send_through(stub)
    # Segment 0 is acked in the first round; then 1 + SEGMENT_RETRANSMITS rounds without
    # progress, each waiting out its timeout despite the repeated acks.
    rounds = 1 + proxy.SEGMENT_RETRANSMITS
    assert stub.segments == [0, 1, 2, 3, 4, 5] + [1, 2, 3, 4, 5] * rounds


def test_acks_from_another_src_or_for_another_seq_zero_are_ignored(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_ACK_TIMEOUT", 0.02)

    def wrong_acks(seg_o, seq_zero, acked):
        return [(NODE + 5, seq_zero, 0b111111), (NODE, seq_zero ^ 1, 0b111111)]

    stub = AckingProxy(wrong_acks)
    with pytest.raises(MeshTimeout):
        send_through(stub)
    assert len(stub.segments) == 6 * (1 + proxy.SEGMENT_RETRANSMITS)


def test_group_segmented_send_does_not_wait_for_acks(monkeypatch):
    monkeypatch.setattr(proxy, "SEGMENT_GROUP_INTERVAL", 0.01)
    sent = record_network_pdus(monkeypatch)
    sim = SimMesh()

    async def scenario():
        _, _, link = make_link(sim)
        await link.send(0xC000, SIX_SEGMENTS)

    run(scenario(), timeout=0.5)  # no Segment Ack ever comes from a group
    assert [pdu.dst for pdu in sim.from_client] == [0xC000] * 6 * (1 + proxy.SEGMENT_RETRANSMITS)
    assert len(sent) == len(set(sent))  # every send has a fresh seq
