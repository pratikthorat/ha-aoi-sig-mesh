import asyncio

import pytest

from mesh import proxy
from mesh.config import OP_APPKEY_STATUS, STATUS_SUCCESS, config_appkey_add
from mesh.errors import LinkClosedError, MeshError, MeshTimeout
from tests.mesh.test_proxy import make_link
from tests.sim.mesh_sim import APP_KEY, SimMesh, run


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
    assert len(seqs) == len(set(seqs)) == 3  # segment 1, then segments 0 and 1, all new seqs


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

    run(scenario())


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
