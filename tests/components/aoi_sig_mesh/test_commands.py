"""Commands through the coordinator: optimistic state, confirmation, revert and errors."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.mesh import network as network_module
from custom_components.aoi_sig_mesh.mesh.control import NodeCommands
from custom_components.aoi_sig_mesh.mesh.errors import LinkClosedError, MeshTimeout, SeqNotSaved
from custom_components.aoi_sig_mesh.mesh.models import OP_CTL_STATUS, OP_LIGHTNESS_STATUS
from custom_components.aoi_sig_mesh.mesh.proxy import ProxyLink
from custom_components.aoi_sig_mesh.store import NetworkStore
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import make_entry, node_data, setup_entry, until


def lamp(entry: Any) -> tuple[MeshCoordinator, str]:
    coordinator: MeshCoordinator = entry.runtime_data
    return coordinator, next(iter(coordinator.nodes))


async def test_turn_on_with_brightness_and_temperature(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)

    await coordinator.async_turn_on(sid, lightness=0x8000, kelvin=3000)

    assert (bulb.on, bulb.lightness, bulb.kelvin) == (True, 0x8000, 3000)
    state = coordinator.nodes[sid].state
    assert (state.on, state.lightness, state.kelvin) == (True, 0x8000, 3000)


async def test_a_temperature_outside_the_range_is_clamped(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)
    record = coordinator.nodes[sid]

    await coordinator.async_turn_on(sid, kelvin=20000)

    assert record.state.kelvin == record.node.kelvin_max
    assert bulb.kelvin == min(record.node.kelvin_max, bulb.kelvin_max)


async def test_the_status_the_bulb_sends_back_is_the_state(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """Our range says 6500 K but the bulb stops at 5000 K: its CTL Status wins."""
    coordinator, sid = lamp(bulb_entry)
    bulb.kelvin_max = 5000

    await coordinator.async_turn_on(sid, lightness=0x8000, kelvin=6000)

    await until(lambda: coordinator.nodes[sid].state.kelvin == 5000)
    assert bulb.kelvin == 5000


async def test_a_seq_block_still_being_saved_is_awaited_then_the_command_retried(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)
    calls: list[int] = []
    real_turn_on = NodeCommands.turn_on

    async def first_refused(self: NodeCommands, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise SeqNotSaved("pending")
        await real_turn_on(self, **kwargs)

    with patch.object(NodeCommands, "turn_on", first_refused):
        await coordinator.async_turn_on(sid)

    assert len(calls) == 2
    assert coordinator.nodes[sid].state.on is True


async def test_a_seq_block_that_cannot_be_saved_raises_and_reverts(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)
    calls: list[int] = []

    async def always_refused(self: NodeCommands, **kwargs: Any) -> None:
        calls.append(1)
        raise SeqNotSaved("not saved")

    with (
        patch.object(NodeCommands, "turn_on", always_refused),
        pytest.raises(HomeAssistantError) as err,
    ):
        await coordinator.async_turn_on(sid)

    assert len(calls) == 2  # retried once, not forever
    assert err.value.translation_key == "seq_not_saved"
    assert coordinator.nodes[sid].state.on is False
    assert coordinator.nodes[sid].failures == 0  # the bulb did nothing wrong


async def test_restart_never_reuses_seq(
    hass: HomeAssistant,
    sim: SimMesh,
    bulb: SimNode,
    radio: SimRadio,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the last seq ceiling was queued: no seq is ever sent twice.

    Small seq blocks, so the ten commands cross several and the lost shutdown write matters.
    """
    monkeypatch.setattr(network_module, "SEQ_BLOCK", 8)
    monkeypatch.setattr(network_module, "RESERVE_MARGIN", 4)
    radio.advertise()
    entry = make_entry(("Desk lamp", node_data(bulb)))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected and bulb.received)
    record_id = next(iter(coordinator.nodes))
    for _ in range(5):
        await coordinator.async_turn_on(record_id)
        await coordinator.async_turn_off(record_id)
    await hass.async_block_till_done()
    crash_state = dict(hass_storage[f"aoi_sig_mesh.{entry.entry_id}"]["data"])
    used = {pdu.seq for pdu in sim.from_client}

    # Crash: the shutdown write never happens, so only the confirmed seq-block saves survive.
    async def _lost(self: NetworkStore, state: dict[str, Any]) -> None:
        return None

    with patch.object(NetworkStore, "async_save", _lost):
        await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[f"aoi_sig_mesh.{entry.entry_id}"]["data"] = crash_state
    sim.from_client.clear()

    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    await until(lambda: coordinator.connected)
    await coordinator.async_turn_on(record_id)

    assert crash_state["seq"] > max(used)
    assert min(pdu.seq for pdu in sim.from_client) > max(used)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_turn_off(hass: HomeAssistant, bulb: SimNode, bulb_entry: Any) -> None:
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_on(sid)

    await coordinator.async_turn_off(sid, transition_ms=1000)

    assert not bulb.on
    assert coordinator.nodes[sid].state.on is False


async def test_a_command_the_bulb_never_answers_reverts_and_raises(
    hass: HomeAssistant, sim: SimMesh, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_on(sid)
    sim.silent = True

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_turn_off(sid)

    assert err.value.translation_key == "command_failed"
    assert err.value.translation_placeholders == {"name": "Desk lamp"}
    assert coordinator.nodes[sid].state.on is True
    assert coordinator.nodes[sid].failures == 1


async def test_a_command_with_no_proxy_in_range_raises_not_connected(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "COMMAND_CONNECT_TIMEOUT", 0.3)
    coordinator, sid = lamp(bulb_entry)
    radio.forget()
    radio.transports[-1].drop()

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_turn_on(sid)

    assert err.value.translation_key == "not_connected"
    assert coordinator.nodes[sid].state.on is False


async def test_identify_sets_health_attention(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)

    await coordinator.async_identify(sid)

    await until(lambda: bulb.attention == 5)


async def test_reconnect_opens_a_new_connection(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    coordinator, _sid = lamp(bulb_entry)

    coordinator.async_reconnect()

    await until(lambda: len(radio.transports) == 2 and coordinator.connected)


async def test_a_late_failure_never_overwrites_a_newer_confirmed_status(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """A reaches the bulb but its ack is lost; B (50 %) succeeds; then A times out."""
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_off(sid)
    real_turn_on = NodeCommands.turn_on
    gate = asyncio.Event()

    async def ack_lost(self: NodeCommands, **kwargs: Any) -> None:
        if kwargs.get("lightness") is None:
            await real_turn_on(self, **kwargs)  # the bulb applies it
            await gate.wait()
            raise MeshTimeout("ack lost")
        await real_turn_on(self, **kwargs)

    with patch.object(NodeCommands, "turn_on", ack_lost):
        first = hass.async_create_task(coordinator.async_turn_on(sid))
        await until(lambda: bulb.on)
        await coordinator.async_turn_on(sid, lightness=0x8000)
        gate.set()
        with pytest.raises(HomeAssistantError):
            await first

    state = coordinator.nodes[sid].state
    assert (bulb.on, bulb.lightness) == (True, 0x8000)
    assert (state.on, state.lightness) == (True, 0x8000)


async def test_a_set_that_times_out_after_its_onoff_was_confirmed_keeps_the_confirmed_state(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """Tuya: OnOff ON is acknowledged, then the Lightness Set goes unanswered."""
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_off(sid)
    record = coordinator.nodes[sid]
    real_request = ProxyLink.request

    async def level_lost(
        self: ProxyLink, dst: int, access: bytes, response_opcode: int, **kwargs: Any
    ) -> Any:
        if response_opcode in (OP_LIGHTNESS_STATUS, OP_CTL_STATUS):
            raise MeshTimeout("no answer")
        return await real_request(self, dst, access, response_opcode, **kwargs)

    with (
        patch.object(ProxyLink, "request", level_lost),
        pytest.raises(HomeAssistantError) as err,
    ):
        await coordinator.async_turn_on(sid, lightness=0x8000)

    assert err.value.translation_key == "command_failed"
    assert bulb.on is True
    assert record.state.on is True  # the OnOff Status said so: not reverted to off
    assert record.state == record.confirmed  # the unconfirmed 50 % is dropped
    assert record.state.lightness != 0x8000
    assert record.failures == 1


async def test_a_pending_command_does_not_make_the_next_one_skip_onoff_before_set(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """A plain turn-on is still waiting; a 50 % turn-on must still send OnOff ON first."""
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_off(sid)
    real_turn_on = NodeCommands.turn_on
    gate = asyncio.Event()

    async def plain_waits(self: NodeCommands, **kwargs: Any) -> None:
        if kwargs.get("lightness") is None:
            await gate.wait()
        await real_turn_on(self, **kwargs)

    with patch.object(NodeCommands, "turn_on", plain_waits):
        first = hass.async_create_task(coordinator.async_turn_on(sid))
        await until(lambda: coordinator.nodes[sid].state.on is True)
        await coordinator.async_turn_on(sid, lightness=0x8000)
        assert (bulb.on, bulb.lightness) == (True, 0x8000)
        gate.set()
        await first


async def test_a_cancelled_command_reverts(hass: HomeAssistant, bulb_entry: Any) -> None:
    coordinator, sid = lamp(bulb_entry)
    started = asyncio.Event()

    async def hangs(self: NodeCommands, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(NodeCommands, "turn_on", hangs):
        command = hass.async_create_task(coordinator.async_turn_on(sid))
        await until(started.is_set)
        assert coordinator.nodes[sid].state.on is True
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

    assert coordinator.nodes[sid].state.on is False


async def test_a_link_lost_mid_command_raises_not_connected(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    coordinator, sid = lamp(bulb_entry)

    async def dropped(self: NodeCommands, **kwargs: Any) -> None:
        raise LinkClosedError("proxy link closed")

    with (
        patch.object(NodeCommands, "turn_on", dropped),
        pytest.raises(HomeAssistantError) as err,
    ):
        await coordinator.async_turn_on(sid)

    assert err.value.translation_key == "not_connected"
    assert coordinator.nodes[sid].state.on is False
    assert coordinator.nodes[sid].failures == 0


async def test_a_command_waiting_for_a_link_fails_at_once_when_the_entry_unloads(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "COMMAND_CONNECT_TIMEOUT", 30.0)
    coordinator, sid = lamp(bulb_entry)
    radio.forget()
    radio.transports[-1].drop()
    await until(lambda: not coordinator.connected)
    command = hass.async_create_task(coordinator.async_turn_on(sid))
    await until(lambda: coordinator.nodes[sid].state.on is True)

    await hass.config_entries.async_unload(bulb_entry.entry_id)

    with pytest.raises(HomeAssistantError) as err:
        async with asyncio.timeout(1.0):
            await command
    assert err.value.translation_key == "not_connected"
    assert coordinator.nodes[sid].state.on is False
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_turn_on(sid)  # after unload: refused without waiting
    assert err.value.translation_key == "not_connected"


async def test_a_save_that_never_finishes_raises_seq_not_saved(
    hass: HomeAssistant, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "SAVE_WAIT", 0.3)
    coordinator, sid = lamp(bulb_entry)
    stuck = asyncio.Event()

    async def hung(self: NetworkStore, state: dict[str, Any]) -> None:
        await stuck.wait()

    network = coordinator.network
    network._seq = network._ceiling  # the saved block is used up: the next send needs a new one
    with patch.object(NetworkStore, "async_save", hung):
        try:
            with pytest.raises(HomeAssistantError) as err:
                await coordinator.async_turn_on(sid)
        finally:
            stuck.set()  # let the write finish, so the unload's final save is not stuck
            await coordinator._saver.async_flush()

    assert err.value.translation_key == "seq_not_saved"
    assert coordinator.nodes[sid].state.on is False
    assert coordinator.nodes[sid].failures == 0


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")], ids=["A-then-B", "B-then-A"])
async def test_two_overlapping_failures(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any, order: tuple[str, str]
) -> None:
    """A (plain turn-on) and B (50 %) overlap and both go unanswered: the bulb stayed off."""
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_off(sid)
    record = coordinator.nodes[sid]
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    entered: set[str] = set()

    async def unanswered(self: NodeCommands, **kwargs: Any) -> None:
        name = "A" if kwargs.get("lightness") is None else "B"
        entered.add(name)
        await gates[name].wait()  # nothing reaches the bulb; no real timeout is driven
        raise MeshTimeout("no answer")

    with patch.object(NodeCommands, "turn_on", unanswered):
        commands = {"A": hass.async_create_task(coordinator.async_turn_on(sid))}
        await until(lambda: "A" in entered)
        commands["B"] = hass.async_create_task(coordinator.async_turn_on(sid, lightness=0x8000))
        await until(lambda: "B" in entered)
        assert record.state.on is True  # both optimistic
        for name in order:
            gates[name].set()
            with pytest.raises(HomeAssistantError):
                await commands[name]

    assert bulb.on is False
    assert record.state.on is False
    assert record.state == record.confirmed


async def test_an_older_failure_under_a_newer_success_settles_on_what_the_bulb_said(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """A (75 %) fails while B (4000 K only) is pending; B's status carries only the temperature.

    A's optimistic brightness must not outlive B: the bulb is still at 25 %.
    """
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_on(sid, lightness=0x4000, kelvin=3000)
    record = coordinator.nodes[sid]
    real_turn_on = NodeCommands.turn_on
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    entered: set[str] = set()

    async def gated(self: NodeCommands, **kwargs: Any) -> None:
        name = "A" if kwargs.get("lightness") is not None else "B"
        entered.add(name)
        await gates[name].wait()
        if name == "A":
            raise MeshTimeout("no answer")
        await real_turn_on(self, **kwargs)

    with patch.object(NodeCommands, "turn_on", gated):
        commands = {"A": hass.async_create_task(coordinator.async_turn_on(sid, lightness=0xBFFF))}
        await until(lambda: "A" in entered)
        commands["B"] = hass.async_create_task(coordinator.async_turn_on(sid, kelvin=4000))
        await until(lambda: "B" in entered)
        assert (record.state.lightness, record.state.kelvin) == (0xBFFF, 4000)  # both optimistic
        gates["A"].set()
        with pytest.raises(HomeAssistantError):
            await commands["A"]
        gates["B"].set()
        await commands["B"]

    assert (bulb.lightness, bulb.kelvin) == (0x4000, 4000)
    assert record.state == record.confirmed
    assert (record.state.lightness, record.state.kelvin) == (0x4000, 4000)

    # The settled state is a copy of confirmed: a failing turn-off's optimism (and its revert)
    # must not reach what the bulb said.
    async def unanswered(self: NodeCommands, **kwargs: Any) -> None:
        raise MeshTimeout("no answer")

    with patch.object(NodeCommands, "turn_off", unanswered), pytest.raises(HomeAssistantError):
        await coordinator.async_turn_off(sid)
    assert record.confirmed.on is True
    assert record.state.on is True


async def test_an_older_success_keeps_a_newer_pending_commands_optimism(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    """A (4000 K) succeeds while B (75 %) is pending: B's brightness stays until B is answered."""
    coordinator, sid = lamp(bulb_entry)
    await coordinator.async_turn_on(sid, lightness=0x4000, kelvin=3000)
    record = coordinator.nodes[sid]
    real_turn_on = NodeCommands.turn_on
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    entered: set[str] = set()

    async def gated(self: NodeCommands, **kwargs: Any) -> None:
        name = "A" if kwargs.get("lightness") is None else "B"
        entered.add(name)
        await gates[name].wait()
        await real_turn_on(self, **kwargs)

    with patch.object(NodeCommands, "turn_on", gated):
        commands = {"A": hass.async_create_task(coordinator.async_turn_on(sid, kelvin=4000))}
        await until(lambda: "A" in entered)
        commands["B"] = hass.async_create_task(coordinator.async_turn_on(sid, lightness=0xBFFF))
        await until(lambda: "B" in entered)
        gates["A"].set()
        await commands["A"]
        assert bulb.kelvin == 4000 and bulb.lightness == 0x4000  # A done, B not sent yet
        assert record.confirmed.lightness == 0x4000
        assert (record.state.lightness, record.state.kelvin) == (0xBFFF, 4000)  # not settled
        gates["B"].set()
        await commands["B"]

    assert (bulb.lightness, bulb.kelvin) == (0xBFFF, 4000)
    assert record.state == record.confirmed
    assert (record.state.lightness, record.state.kelvin) == (0xBFFF, 4000)
