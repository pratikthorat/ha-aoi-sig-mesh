import asyncio

import pytest

from mesh import connection, proxy
from mesh.connection import ConnectionManager
from mesh.errors import LinkClosedError, MeshTimeout
from mesh.models import OP_ONOFF_STATUS, onoff_get
from mesh.network import MeshNetwork
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, run
from tests.sim.sim_radio import BULB_MAC, SimRadio

OTHER_NETWORK = bytes.fromhex("0102030405060708")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr(proxy, "FILTER_TIMEOUT", 0.1)
    monkeypatch.setattr(connection, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(connection, "BACKOFF_MAX", 0.4)


class Harness:
    def __init__(self, *, idle_timeout=0.0, node_macs=()):
        self.sim = SimMesh()
        self.bulb = self.sim.add_node("ctl")
        self.radio = SimRadio(self.sim)
        self.network = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=CLIENT)
        self.connected = []
        self.disconnects = 0
        self.manager = ConnectionManager(
            self.network,
            self.radio,
            dev_key_for=self.sim.dev_key_for,
            node_addresses=lambda: set(node_macs),
            on_connected=self._connected,
            on_disconnected=self._disconnected,
            idle_timeout=idle_timeout,
        )

    async def _connected(self, link, candidate):
        self.connected.append(candidate.address)

    def _disconnected(self):
        self.disconnects += 1

    async def until(self, condition, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)


def test_connects_to_the_strongest_proxy_of_our_network():
    async def scenario():
        h = Harness()
        h.radio.advertise("AA:00:00:00:00:01", rssi=-80)
        h.radio.advertise("AA:00:00:00:00:02", rssi=-50)
        h.radio.advertise("AA:00:00:00:00:03", rssi=-30, network_id=OTHER_NETWORK)
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        assert h.connected == ["AA:00:00:00:00:02"]
        assert h.manager.proxy.address == "AA:00:00:00:00:02"
        reply = await h.manager.link.request(0x00B0, onoff_get(), OP_ONOFF_STATUS)
        assert reply.params == b"\x00"
        await h.manager.stop()
        assert h.manager.link is None
        assert h.radio.subscribers == 0

    run(scenario())


def test_falls_back_to_the_next_proxy_when_the_strongest_fails():
    async def scenario():
        h = Harness()
        h.radio.advertise("AA:00:00:00:00:01", rssi=-80)
        h.radio.advertise("AA:00:00:00:00:02", rssi=-50)
        h.radio.unreachable.add("AA:00:00:00:00:02")
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        assert h.radio.connects == ["AA:00:00:00:00:02", "AA:00:00:00:00:01"]
        await h.manager.stop()

    run(scenario())


def test_node_identity_advert_from_a_known_node_is_used():
    async def scenario():
        h = Harness(node_macs={BULB_MAC})
        h.radio.advertise("AA:00:00:00:00:09", identity=True)
        h.radio.advertise(BULB_MAC, identity=True, rssi=-90)
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        assert h.radio.connects == [BULB_MAC]
        await h.manager.stop()

    run(scenario())


def test_reconnects_after_the_proxy_drops():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        first = h.radio.transports[0]
        first.drop()
        await h.until(lambda: h.disconnects == 1)
        assert h.manager.link is None
        await h.until(lambda: len(h.connected) == 2)
        assert h.manager.link is not None
        assert h.radio.transports[1] is not first
        await h.manager.stop()
        assert h.disconnects == 2

    run(scenario())


def test_a_failed_write_closes_the_transport():
    """ProxyLink leaves the transport open when a write raises; the manager must close it."""

    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        first = h.radio.transports[0]
        closed = []

        async def broken_write(data):
            raise OSError("adapter gone")

        async def close():
            closed.append(True)
            first.drop()

        first.write = broken_write
        first.close = close
        with pytest.raises(LinkClosedError):
            await h.manager.link.request(0x00B0, onoff_get(), OP_ONOFF_STATUS)
        await h.until(lambda: closed)
        await h.until(lambda: len(h.connected) == 2)
        await h.manager.stop()

    run(scenario())


def test_an_advert_storm_does_not_defeat_the_backoff():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.radio.fail_connects = 1000
        h.manager.start()
        await h.until(lambda: len(h.radio.connects) == 1)
        for _ in range(60):
            h.radio.advertise()
            await asyncio.sleep(0.005)
        # attempts at ~0, 0.05, 0.15 and 0.35 s: a storm-driven loop would show dozens
        assert len(h.radio.connects) <= 4
        await h.manager.stop()

    run(scenario())


def test_a_new_proxy_wakes_a_sleeping_loop(monkeypatch):
    async def scenario():
        monkeypatch.setattr(connection, "BACKOFF_MIN", 30.0)
        h = Harness()
        h.manager.start()
        await asyncio.sleep(0.05)
        assert h.radio.connects == []
        h.radio.advertise()
        await h.until(lambda: h.manager.link is not None, timeout=1.0)
        await h.manager.stop()

    run(scenario())


def test_stale_close_callback_from_an_old_link_is_ignored():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        old = h.manager.link
        h.manager.reconnect()
        await h.until(lambda: len(h.connected) == 2)
        new = h.manager.link
        assert new is not old
        old._on_close()  # what a late callback from the first link would do
        await asyncio.sleep(0.1)
        assert h.manager.link is new
        assert h.disconnects == 1
        await h.manager.stop()

    run(scenario())


def test_idle_policy_disconnects_and_connects_on_demand():
    async def scenario():
        h = Harness(idle_timeout=0.1)
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: len(h.connected) == 1)
        await h.until(lambda: h.manager.idle)
        assert h.manager.link is None
        assert h.disconnects == 1
        link = await h.manager.get_link(timeout=1.0)
        assert link.is_connected
        assert len(h.connected) == 2
        await h.manager.stop()

    run(scenario())


def test_get_link_times_out_without_a_proxy():
    async def scenario():
        h = Harness()
        h.manager.start()
        with pytest.raises(MeshTimeout):
            await h.manager.get_link(timeout=0.1)
        await h.manager.stop()

    run(scenario())
