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
