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
    def __init__(self, *, idle_timeout=0.0, node_macs=(), on_connected=None, on_disconnected=None):
        self.extra_connected = on_connected
        self.extra_disconnected = on_disconnected
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
        if self.extra_connected is not None:
            await self.extra_connected(link, candidate)

    def _disconnected(self):
        self.disconnects += 1
        if self.extra_disconnected is not None:
            self.extra_disconnected()

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


def _track_closes(radio):
    """Record every transport close() the manager awaits."""
    closes = []
    connect = radio.connect

    async def connect_and_track(address):
        transport = await connect(address)
        original = transport.close

        async def close():
            closes.append(address)
            await original()

        transport.close = close
        return transport

    radio.connect = connect_and_track
    return closes


def test_a_close_that_raises_does_not_kill_the_loop():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        first = h.radio.transports[0]
        original = first.close

        async def bad_close():
            await original()
            raise OSError("bleak disconnect failed")

        first.close = bad_close
        first.drop()
        await h.until(lambda: len(h.connected) == 2)
        await h.manager.stop()

    run(scenario())


def test_an_on_disconnected_that_raises_does_not_kill_the_loop():
    async def scenario():
        def boom():
            raise RuntimeError("coordinator bug")

        h = Harness(on_disconnected=boom)
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        h.radio.transports[0].drop()
        await h.until(lambda: len(h.connected) == 2)
        await h.manager.stop()

    run(scenario())


def test_a_failing_candidate_lookup_is_survived(monkeypatch):
    async def scenario():
        h = Harness()
        h.radio.advertise()
        real = h.manager.candidates
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("radio gone")
            return real()

        monkeypatch.setattr(h.manager, "candidates", flaky)
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        await h.manager.stop()

    run(scenario())


@pytest.mark.parametrize("failure", ["raises", "times_out"])
def test_a_link_that_fails_to_start_closes_its_transport(monkeypatch, failure):
    async def scenario():
        monkeypatch.setattr(connection, "CONNECT_TIMEOUT", 0.1)
        h = Harness()
        closes = _track_closes(h.radio)
        h.radio.advertise()
        real_start = proxy.ProxyLink.start
        attempts = []

        async def bad_start(self, **kwargs):
            attempts.append(1)
            if len(attempts) > 1:
                return await real_start(self, **kwargs)
            if failure == "raises":
                raise OSError("notify failed")
            await asyncio.sleep(10)

        monkeypatch.setattr(proxy.ProxyLink, "start", bad_start)
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        assert closes == [BULB_MAC]  # the failed attempt's transport, closed
        await h.manager.stop()
        assert closes == [BULB_MAC, BULB_MAC]

    run(scenario())


def test_a_flapping_link_backs_off():
    async def scenario():
        async def drop_at_once(link, candidate):
            h.radio.transports[-1].drop()

        h = Harness(on_connected=drop_at_once)
        h.radio.advertise()
        h.manager.start()
        await asyncio.sleep(1.0)
        # waits of 0.05, 0.1, 0.2, 0.4 s: a fixed 0.05 s retry would show about 19 connects
        assert len(h.radio.connects) < 8
        await h.manager.stop()

    run(scenario())


def test_a_link_that_held_resets_the_backoff(monkeypatch):
    async def scenario():
        monkeypatch.setattr(connection, "BACKOFF_MAX", 0.1)
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        await asyncio.sleep(0.15)  # up for longer than BACKOFF_MAX
        h.radio.transports[0].drop()
        await h.until(lambda: len(h.connected) == 2)
        await h.manager.stop()

    run(scenario())


def test_stop_leaves_no_tasks_behind():
    async def scenario():
        h = Harness(idle_timeout=0.1)
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        await h.manager.get_link(1.0)
        await h.manager.stop()
        await asyncio.sleep(0)
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    run(scenario())


def test_start_twice_runs_one_loop():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        first = h.manager._task
        h.manager.start()
        assert h.manager._task is first
        assert h.radio.subscribers == 1
        await h.until(lambda: h.manager.link is not None)
        await h.manager.stop()
        assert first.done()
        assert h.radio.subscribers == 0
        assert h.radio.connects == [BULB_MAC]

    run(scenario())


def test_stop_during_teardown_still_reports_the_disconnect_and_finishes_the_close():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        h.manager.start()
        await h.until(lambda: h.manager.link is not None)
        first = h.radio.transports[0]
        finished = []

        async def slow_close():
            await asyncio.sleep(0.2)
            finished.append(True)

        first.close = slow_close
        first.drop()
        await h.until(lambda: h.disconnects == 1)  # now the manager is awaiting the close
        await h.manager.stop()
        assert finished == [True]
        assert h.disconnects == 1

    run(scenario())


def test_reconnect_while_connecting_is_not_lost():
    async def scenario():
        h = Harness()
        h.radio.advertise()
        connect = h.radio.connect
        gate = asyncio.Event()

        async def slow_connect(address):
            await gate.wait()
            return await connect(address)

        h.radio.connect = slow_connect
        h.manager.start()
        await asyncio.sleep(0.05)
        h.manager.reconnect()  # the link is not up yet
        gate.set()
        await h.until(lambda: len(h.connected) == 2)
        await h.until(lambda: h.manager.link is not None)
        assert h.manager._skip_delay is False
        await h.manager.stop()

    run(scenario())
