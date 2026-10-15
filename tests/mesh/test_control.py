import asyncio

import pytest

from mesh import control, proxy
from mesh.control import NodeCommands, NodeState, StatusUpdate, TidCounter, apply_status
from mesh.errors import MeshError, MeshTimeout
from mesh.models import (
    OP_CTL_SET,
    OP_CTL_STATUS,
    OP_CTL_TEMP_SET,
    OP_CTL_TEMP_STATUS,
    OP_HEALTH_ATTENTION_SET_UNACK,
    OP_LIGHTNESS_SET,
    OP_LIGHTNESS_STATUS,
    OP_ONOFF_SET,
    OP_ONOFF_SET_UNACK,
    OP_ONOFF_STATUS,
    ctl_set,
    onoff_set,
)
from mesh.network import MeshNetwork
from mesh.node import MeshNode
from mesh.proxy import AccessMessage, ProxyLink
from mesh.quirks import Quirk, quirks_for
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, run


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(control, "BLINK_INTERVAL", 0.0)


def node_for(sim_node, **overrides) -> MeshNode:
    node = MeshNode(unicast=sim_node.unicast, element_count=1, dev_key=sim_node.dev_key)
    node.apply_composition(sim_node.composition())
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


async def open_link(sim, state=None):
    network = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=CLIENT)
    state = NodeState() if state is None else state
    link = ProxyLink(
        network,
        sim.connect(),
        dev_key_for=sim.dev_key_for,
        on_message=lambda message: apply_status(state, message),
    )
    await link.start()
    return link, state


def sets(sim_node):
    return [(dst, opcode) for dst, opcode, _ in sim_node.received if opcode != 0x8201]


def msg(opcode, params):
    return AccessMessage(0x00B0, CLIENT, opcode, bytes.fromhex(params), dev_key=False)


def test_apply_status_prefers_target_and_keeps_lightness_while_off():
    state = NodeState()
    assert apply_status(state, msg(OP_LIGHTNESS_STATUS, "0080")) == StatusUpdate(None)
    assert (state.on, state.lightness) == (True, 0x8000)
    update = apply_status(state, msg(OP_LIGHTNESS_STATUS, "00800000" + "0a"))
    assert update == StatusUpdate(1000)
    assert (state.on, state.lightness) == (False, 0x8000)
    apply_status(state, msg(OP_CTL_STATUS, "ffffa00f"))
    assert (state.on, state.lightness, state.kelvin) == (True, 0xFFFF, 4000)
    apply_status(state, msg(OP_CTL_TEMP_STATUS, "8c0a0000"))
    assert state.kelvin == 2700
    apply_status(state, msg(OP_ONOFF_STATUS, "00"))
    assert state.on is False


def test_apply_status_ignores_other_and_malformed_messages():
    state = NodeState()
    assert apply_status(state, msg(0x8007, "05")) is None
    assert apply_status(state, msg(OP_LIGHTNESS_STATUS, "00")) is None
    assert state == NodeState()


def test_tid_counter_counts_up_per_destination_and_wraps():
    tids = TidCounter(start=254)
    assert [tids.next(0x00B0), tids.next(0x00B0), tids.next(0x00B0)] == [255, 0, 1]
    assert [tids.next(0x00B1), tids.next(0x00B0)] == [255, 2]


def test_tid_counter_never_repeats_a_tid_to_one_destination_within_six_seconds():
    now = [100.0]
    tids = TidCounter(start=0, clock=lambda: now[0])
    seen = [tids.next(0x00B0) for _ in range(256)]
    assert len(set(seen)) == 256
    with pytest.raises(MeshError):
        tids.next(0x00B0)  # 257 commands in under 6 s would reuse a TID
    assert tids.next(0x00C0) == 1  # other destinations are unaffected
    now[0] += 6.0
    assert tids.next(0x00B0) == seen[0]


def test_the_sim_does_not_reapply_a_repeated_tid_but_still_answers():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", configured=True)
        link, state = await open_link(sim)
        await link.request(bulb.unicast, onoff_set(True, 7), OP_ONOFF_STATUS)
        bulb.on = False  # a wall switch, between the two copies
        reply = await link.request(bulb.unicast, onoff_set(True, 7), OP_ONOFF_STATUS)
        assert reply.params[0] == 0  # answered with the current state, not applied again
        assert (bulb.on, bulb.retransmissions) == (False, 1)
        await link.request(bulb.unicast, onoff_set(True, 8), OP_ONOFF_STATUS)
        assert bulb.on
        await link.close()

    run(scenario())


def test_the_sim_reads_the_tid_where_each_set_carries_it():
    """CTL Set has its TID after Delta UV; reading Delta UV instead made every CTL Set
    after the first look like a retransmission."""

    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", configured=True)
        link, state = await open_link(sim)
        await link.request(bulb.unicast, ctl_set(0x8000, 3000, 1), OP_CTL_STATUS)
        await link.request(bulb.unicast, ctl_set(0x8000, 4000, 2), OP_CTL_STATUS)
        assert (bulb.kelvin, bulb.retransmissions) == (4000, 0)
        await link.close()

    run(scenario())


def test_tuya_brightness_while_off_sends_onoff_first():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", tuya_quirks=True)
        link, state = await open_link(sim)
        node = node_for(bulb)
        commands = NodeCommands(link, node, quirks_for(node.cid, node.pid), TidCounter())
        await commands.turn_on(currently_on=False, lightness=0x8000)
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET), (0x00B0, OP_LIGHTNESS_SET)]
        assert (bulb.on, bulb.lightness) == (True, 0x8000)
        assert (state.on, state.lightness) == (True, 0x8000)

    run(scenario())


def test_tuya_turn_off_uses_generic_onoff():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", tuya_quirks=True)
        bulb.on = True
        link, state = await open_link(sim)
        node = node_for(bulb)
        await NodeCommands(link, node, quirks_for(node.cid, node.pid), TidCounter()).turn_off()
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET)]
        assert bulb.on is False
        assert state.on is False

    run(scenario())


def test_spec_light_turns_off_with_lightness_zero_and_on_with_a_set():
    async def scenario():
        sim = SimMesh()
        lamp = sim.add_node("lightness")
        lamp.on = True
        link, state = await open_link(sim)
        commands = NodeCommands(link, node_for(lamp), Quirk.NONE, TidCounter())
        await commands.turn_off()
        assert lamp.on is False
        assert state.on is False
        await commands.turn_on(currently_on=False, lightness=0x4000)
        assert sets(lamp) == [(0x00B0, OP_LIGHTNESS_SET), (0x00B0, OP_LIGHTNESS_SET)]
        assert (lamp.on, lamp.lightness) == (True, 0x4000)

    run(scenario())


def test_temperature_only_while_off_turns_on_first_and_clamps():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        link, state = await open_link(sim)
        commands = NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter())
        await commands.turn_on(currently_on=False, kelvin=9000)
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET), (0x00B1, OP_CTL_TEMP_SET)]
        assert (bulb.on, bulb.kelvin) == (True, 6500)
        assert state.kelvin == 6500

    run(scenario())


def test_brightness_and_temperature_together_use_one_ctl_set():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.on = True
        link, state = await open_link(sim)
        commands = NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter())
        await commands.turn_on(currently_on=True, lightness=0x2000, kelvin=3000, transition_ms=500)
        assert sets(bulb) == [(0x00B0, OP_CTL_SET)]
        assert (bulb.lightness, bulb.kelvin) == (0x2000, 3000)
        assert (state.lightness, state.kelvin) == (0x2000, 3000)

    run(scenario())


def test_plug_only_ever_gets_onoff():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff")
        link, state = await open_link(sim)
        commands = NodeCommands(link, node_for(plug), Quirk.NONE, TidCounter())
        await commands.turn_on(currently_on=False, lightness=0x2000, kelvin=3000)
        await commands.turn_off()
        assert sets(plug) == [(0x00B0, OP_ONOFF_SET), (0x00B0, OP_ONOFF_SET)]
        assert state.on is False

    run(scenario())


def test_refresh_reads_onoff_and_ctl():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.on, bulb.lightness, bulb.kelvin = True, 0x1234, 3100
        link, state = await open_link(sim)
        answered = await NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter()).refresh()
        assert answered == 2
        assert (state.on, state.lightness, state.kelvin) == (True, 0x1234, 3100)

    run(scenario())


def test_refresh_of_a_silent_node_raises_timeout():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        link, _ = await open_link(sim)
        sim.silent = True
        with pytest.raises(MeshTimeout):
            await NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter()).refresh()

    run(scenario(), timeout=10)


def test_identify_uses_health_attention_when_present():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        link, _ = await open_link(sim)
        await NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter()).identify(
            currently_on=False
        )
        await asyncio.sleep(0.01)
        assert sets(bulb) == [(0x00B0, OP_HEALTH_ATTENTION_SET_UNACK)]
        assert bulb.attention == 5

    run(scenario())


def test_identify_blinks_a_light_without_health_and_restores_it():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.on = True
        link, _ = await open_link(sim)
        node = node_for(bulb)
        node.models.pop("health")
        await NodeCommands(link, node, Quirk.NONE, TidCounter()).identify(currently_on=True)
        await asyncio.sleep(0.01)
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET_UNACK)] * 6
        assert bulb.on is True

    run(scenario())


def test_identify_refuses_a_plug_without_health():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff")
        link, _ = await open_link(sim)
        with pytest.raises(ValueError):
            await NodeCommands(link, node_for(plug), Quirk.NONE, TidCounter()).identify(
                currently_on=True
            )
        assert sets(plug) == []

    run(scenario())
