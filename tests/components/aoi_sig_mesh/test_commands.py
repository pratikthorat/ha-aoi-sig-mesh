"""Commands through the coordinator: optimistic state, confirmation, revert and errors."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.aoi_sig_mesh import coordinator as coordinator_module
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from custom_components.aoi_sig_mesh.mesh.control import NodeCommands
from custom_components.aoi_sig_mesh.mesh.errors import SeqNotSaved
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
) -> None:
    """A crash after the last seq ceiling was queued: no seq is ever sent twice."""
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
