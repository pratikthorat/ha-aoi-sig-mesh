import asyncio

import pytest

from mesh import control, proxy
from mesh.config import OP_NODE_RESET
from mesh.control import (
    NodeCommands,
    NodeState,
    StatusUpdate,
    TidCounter,
    apply_status,
    status_is_for,
)
from mesh.errors import MeshError, MeshTimeout
from mesh.models import (
    OP_CTL_SET,
    OP_CTL_STATUS,
    OP_CTL_TEMP_SET,
    OP_CTL_TEMP_STATUS,
    OP_HEALTH_ATTENTION_SET,
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
    monkeypatch.setattr(control, "ATTENTION_TIMEOUT", 0.1)


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


@pytest.mark.parametrize(
    ("opcode", "params"),
    [
        (OP_ONOFF_STATUS, ""),
        (OP_ONOFF_STATUS, "0001"),
        (OP_ONOFF_STATUS, "00010a00"),
        (OP_CTL_STATUS, "ffffa0"),
        (OP_CTL_STATUS, "ffffa00f00"),
        (OP_CTL_STATUS, "ffffa00fffffa00f"),
        (OP_CTL_TEMP_STATUS, "8c0a00"),
        (OP_CTL_TEMP_STATUS, "8c0a000000"),
        (OP_CTL_TEMP_STATUS, "8c0a00008c0a0000"),
    ],
)
def test_apply_status_ignores_a_status_of_the_wrong_length(opcode, params):
    state = NodeState()
    assert apply_status(state, msg(opcode, params)) is None
    assert state == NodeState()


@pytest.mark.parametrize(
    ("opcode", "params", "expected", "remaining_ms"),
    [
        # present off, target on, 0x05 = 5 x 100 ms
        (OP_ONOFF_STATUS, "000105", NodeState(on=True), 500),
        # 0x3F steps means the remaining time is unknown
        (OP_ONOFF_STATUS, "00013f", NodeState(on=True), None),
        (OP_ONOFF_STATUS, "01007f", NodeState(on=False), None),
        # present 0x1000 / 2700 K, target 0x8000 / 4000 K, 0x41 = 1 x 1 s
        (OP_CTL_STATUS, "00108c0a0080a00f41", NodeState(True, 0x8000, 4000), 1000),
        (OP_CTL_STATUS, "00108c0a0000a00f3f", NodeState(False, None, 4000), None),
        # present 2700 K, target 6500 K (Delta UV 0), 0x81 = 1 x 10 s
        (OP_CTL_TEMP_STATUS, "8c0a0000641900" + "0081", NodeState(kelvin=6500), 10_000),
        (OP_CTL_TEMP_STATUS, "8c0a0000641900" + "00ff", NodeState(kelvin=6500), None),
    ],
)
def test_apply_status_reads_the_long_forms_and_prefers_the_target(
    opcode, params, expected, remaining_ms
):
    state = NodeState()
    assert apply_status(state, msg(opcode, params)) == StatusUpdate(remaining_ms)
    assert state == expected


def test_a_status_counts_only_from_the_element_hosting_its_model():
    sim = SimMesh()
    bulb = node_for(sim.add_node("ctl"))  # OnOff, Lightness, CTL on 0x00B0; CTL Temp on 0x00B1

    def status(src, opcode, params="01"):
        return AccessMessage(src, CLIENT, opcode, bytes.fromhex(params), dev_key=False)

    assert status_is_for(bulb, status(0x00B0, OP_ONOFF_STATUS))
    assert not status_is_for(bulb, status(0x00B1, OP_ONOFF_STATUS))
    assert status_is_for(bulb, status(0x00B1, OP_CTL_TEMP_STATUS))
    assert not status_is_for(bulb, status(0x00B0, OP_CTL_TEMP_STATUS))
    assert not status_is_for(bulb, status(0x00B1, OP_LIGHTNESS_STATUS))
    assert status_is_for(bulb, status(0x00B1, 0x8007))  # not a state status: not checked
    # An imported node knows only its unicast: commands and their answers use it.
    bare = MeshNode(unicast=0x00B0, element_count=1, dev_key=bytes(16))
    assert status_is_for(bare, status(0x00B0, OP_ONOFF_STATUS))


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


def test_tuya_brightness_and_temperature_while_off_sends_onoff_first():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", tuya_quirks=True)
        link, state = await open_link(sim)
        node = node_for(bulb)
        commands = NodeCommands(link, node, quirks_for(node.cid, node.pid), TidCounter())
        await commands.turn_on(currently_on=False, lightness=0x8000, kelvin=3000)
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET), (0x00B0, OP_CTL_SET)]
        assert (bulb.on, bulb.lightness, bulb.kelvin) == (True, 0x8000, 3000)
        assert (state.on, state.lightness, state.kelvin) == (True, 0x8000, 3000)

    run(scenario())


def test_tuya_brightness_while_already_on_skips_onoff():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", tuya_quirks=True)
        bulb.on = True
        link, state = await open_link(sim)
        node = node_for(bulb)
        commands = NodeCommands(link, node, quirks_for(node.cid, node.pid), TidCounter())
        await commands.turn_on(currently_on=True, lightness=0x4000)
        assert sets(bulb) == [(0x00B0, OP_LIGHTNESS_SET)]
        assert (state.on, state.lightness) == (True, 0x4000)

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


def test_onoff_only_turns_a_dimmable_light_off_with_generic_onoff():
    async def scenario():
        sim = SimMesh()
        lamp = sim.add_node("lightness")
        lamp.on = True
        link, state = await open_link(sim)
        commands = NodeCommands(link, node_for(lamp), Quirk.NONE, TidCounter())
        await commands.turn_off(onoff_only=True)
        assert sets(lamp) == [(0x00B0, OP_ONOFF_SET)]
        assert lamp.on is False
        assert state.on is False

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


def test_identify_uses_acknowledged_health_attention_when_bound():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        link, _ = await open_link(sim)
        await NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter()).identify(
            currently_on=False
        )
        await asyncio.sleep(0.01)
        assert sets(bulb) == [(0x00B0, OP_HEALTH_ATTENTION_SET)]  # answered: no blink
        assert bulb.attention == 5

    run(scenario())


def test_identify_blinks_a_light_whose_health_is_not_bound_and_restores_it():
    """Real nodes drop AppKey messages to an unbound model: no answer means blink instead."""

    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.bindings.discard((0x00B0, 0x0002))
        bulb.on = True
        link, _ = await open_link(sim)
        await NodeCommands(link, node_for(bulb), Quirk.NONE, TidCounter()).identify(
            currently_on=True
        )
        await asyncio.sleep(0.01)
        attention = [s for s in sets(bulb) if s[1] == OP_HEALTH_ATTENTION_SET]
        assert attention and bulb.attention == 0
        assert sets(bulb)[len(attention) :] == [(0x00B0, OP_ONOFF_SET_UNACK)] * 6
        assert bulb.on is True

    run(scenario())


def test_identify_on_a_plug_whose_health_is_not_bound_fails_without_toggling():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff", health=True)
        plug.bindings.discard((plug.unicast, 0x0002))
        link, _ = await open_link(sim)
        with pytest.raises(MeshTimeout):
            await NodeCommands(link, node_for(plug), Quirk.NONE, TidCounter()).identify(
                currently_on=True
            )
        assert {opcode for _, opcode in sets(plug)} == {OP_HEALTH_ATTENTION_SET}

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


def test_identify_cancelled_mid_blink_restores_the_light(monkeypatch):
    async def scenario():
        monkeypatch.setattr(control, "BLINK_INTERVAL", 5.0)
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.on = True
        link, _ = await open_link(sim)
        node = node_for(bulb)
        node.models.pop("health")
        commands = NodeCommands(link, node, Quirk.NONE, TidCounter())
        task = asyncio.ensure_future(commands.identify(currently_on=True))
        async with asyncio.timeout(1.0):
            while bulb.on:  # the first blink turned it off
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1.0):
            while not bulb.on:
                await asyncio.sleep(0.01)
        assert sets(bulb) == [(0x00B0, OP_ONOFF_SET_UNACK)] * 2
        await link.close()

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


def test_reset_node_waits_for_the_node_reset_status():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff")
        link, _ = await open_link(sim)
        await NodeCommands(link, node_for(plug), Quirk.NONE, TidCounter()).reset_node()
        assert plug.reset
        assert [opcode for _, opcode, _ in plug.received] == [OP_NODE_RESET]

    run(scenario())


def test_reset_node_without_a_status_times_out():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff")
        link, _ = await open_link(sim)
        sim.silent = True
        with pytest.raises(MeshTimeout):
            await NodeCommands(link, node_for(plug), Quirk.NONE, TidCounter()).reset_node()

    run(scenario(), timeout=10)
