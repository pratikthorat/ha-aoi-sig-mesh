"""Node state from status messages, and the commands that drive a light or plug."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import MalformedPDUError, MeshError, MeshTimeout
from .models import (
    OP_CTL_STATUS,
    OP_CTL_TEMP_STATUS,
    OP_LIGHTNESS_STATUS,
    OP_ONOFF_STATUS,
    ctl_get,
    ctl_set,
    ctl_temperature_set,
    health_attention_set,
    lightness_get,
    lightness_set,
    onoff_get,
    onoff_set,
    parse_ctl_status,
    parse_ctl_temperature_status,
    parse_lightness_status,
    parse_onoff_status,
)
from .node import ROLE_CTL, ROLE_CTL_TEMP, ROLE_HEALTH, ROLE_LIGHTNESS, ROLE_ONOFF, MeshNode
from .proxy import AccessMessage, ProxyLink
from .quirks import Quirk

ATTENTION_SECONDS = 5
BLINKS = 3
BLINK_INTERVAL = 0.4  # read at call time, so tests can shorten it
TID_WINDOW = 6.0  # seconds: a repeated src+dst+TID inside it is a retransmission


@dataclass
class NodeState:
    """What we believe a node is doing. None means not known yet."""

    on: bool | None = None
    lightness: int | None = None  # last non-zero lightness, kept while off
    kelvin: int | None = None


@dataclass(frozen=True)
class StatusUpdate:
    remaining_ms: int | None


def _level(present: int, target: int | None) -> int:
    return present if target is None else target


def _apply_lightness(state: NodeState, lightness: int) -> None:
    if lightness == 0:
        state.on = False
    else:
        state.on = True
        state.lightness = lightness


def apply_status(state: NodeState, message: AccessMessage) -> StatusUpdate | None:
    """Fold one status message into ``state``. Returns None for anything that is not a status.

    During a transition the target value wins, so the UI shows where the node is heading.
    Lightness 0 means off; the last non-zero lightness is kept for the next turn-on.
    """
    try:
        if message.opcode == OP_ONOFF_STATUS:
            onoff = parse_onoff_status(message.params)
            state.on = onoff.present if onoff.target is None else onoff.target
            return StatusUpdate(onoff.remaining_ms)
        if message.opcode == OP_LIGHTNESS_STATUS:
            light = parse_lightness_status(message.params)
            _apply_lightness(state, _level(light.present, light.target))
            return StatusUpdate(light.remaining_ms)
        if message.opcode == OP_CTL_STATUS:
            ctl = parse_ctl_status(message.params)
            _apply_lightness(state, _level(ctl.present_lightness, ctl.target_lightness))
            state.kelvin = _level(ctl.present_kelvin, ctl.target_kelvin)
            return StatusUpdate(ctl.remaining_ms)
        if message.opcode == OP_CTL_TEMP_STATUS:
            temp = parse_ctl_temperature_status(message.params)
            state.kelvin = _level(temp.present_kelvin, temp.target_kelvin)
            return StatusUpdate(temp.remaining_ms)
    except MalformedPDUError:
        return None
    return None


class TidCounter:
    """Transaction identifiers for Set messages, counting up separately per destination.

    A node treats a repeated src+dst+TID within 6 s as a retransmission and does not apply it
    again, so a TID is never handed out twice to one destination inside that window.
    """

    def __init__(
        self, start: int | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._start = start
        self._clock = clock
        self._last: dict[int, int] = {}
        self._used: dict[int, dict[int, float]] = {}

    def next(self, dst: int) -> int:
        now = self._clock()
        last = self._last.get(dst)
        if last is None:
            last = secrets.randbelow(256) if self._start is None else self._start & 0xFF
        tid = (last + 1) & 0xFF
        used = self._used.setdefault(dst, {})
        if tid in used and now - used[tid] < TID_WINDOW:
            raise MeshError(f"more than 256 commands to 0x{dst:04X} within {TID_WINDOW:g} s")
        used[tid] = now
        self._last[dst] = tid
        return tid


class NodeCommands:
    """Acknowledged commands for one node over the current proxy link.

    Statuses come back through the link's ``on_message`` callback, so the caller's state is
    updated there. These methods only send and wait for the acknowledgement.
    """

    def __init__(self, link: ProxyLink, node: MeshNode, quirks: Quirk, tids: TidCounter) -> None:
        self._link = link
        self._node = node
        self._quirks = quirks
        self._tids = tids

    def _address(self, role: str) -> int:
        return self._node.models.get(role, self._node.unicast)

    def _clamp(self, kelvin: int) -> int:
        return max(self._node.kelvin_min, min(self._node.kelvin_max, kelvin))

    async def _onoff(self, on: bool, transition_ms: int | None = None) -> AccessMessage:
        address = self._address(ROLE_ONOFF)
        access = onoff_set(on, self._tids.next(address), transition_ms=transition_ms)
        return await self._link.request(address, access, OP_ONOFF_STATUS)

    async def turn_on(
        self,
        *,
        currently_on: bool,
        lightness: int | None = None,
        kelvin: int | None = None,
        transition_ms: int | None = None,
    ) -> None:
        models = self._node.models
        if lightness is not None and ROLE_LIGHTNESS not in models:
            lightness = None
        if kelvin is not None and not self._node.is_tunable:
            kelvin = None
        if lightness is None and kelvin is None:
            await self._onoff(True, transition_ms)
            return
        if kelvin is not None:
            kelvin = self._clamp(kelvin)
        # A Set while off is ignored by Tuya; a temperature-only Set never turns a light on.
        if not currently_on and (Quirk.ON_BEFORE_SET in self._quirks or lightness is None):
            await self._onoff(True)
        if lightness is not None and kelvin is not None:
            address = models[ROLE_CTL]
            tid = self._tids.next(address)
            access = ctl_set(lightness, kelvin, tid, transition_ms=transition_ms)
            await self._link.request(address, access, OP_CTL_STATUS)
        elif lightness is not None:
            address = models[ROLE_LIGHTNESS]
            tid = self._tids.next(address)
            access = lightness_set(lightness, tid, transition_ms=transition_ms)
            await self._link.request(address, access, OP_LIGHTNESS_STATUS)
        elif kelvin is not None:
            address = models[ROLE_CTL_TEMP]
            tid = self._tids.next(address)
            access = ctl_temperature_set(kelvin, tid, transition_ms=transition_ms)
            await self._link.request(address, access, OP_CTL_TEMP_STATUS)

    async def turn_off(self, *, transition_ms: int | None = None) -> None:
        if Quirk.OFF_VIA_ONOFF in self._quirks or ROLE_LIGHTNESS not in self._node.models:
            await self._onoff(False, transition_ms)
            return
        address = self._node.models[ROLE_LIGHTNESS]
        access = lightness_set(0, self._tids.next(address), transition_ms=transition_ms)
        await self._link.request(address, access, OP_LIGHTNESS_STATUS)

    async def refresh(self) -> int:
        """Ask for OnOff, then CTL (or Lightness). Raises MeshTimeout only if nothing answered."""
        models = self._node.models
        gets: list[tuple[int, bytes, int]] = [
            (self._address(ROLE_ONOFF), onoff_get(), OP_ONOFF_STATUS)
        ]
        if ROLE_CTL in models:
            gets.append((models[ROLE_CTL], ctl_get(), OP_CTL_STATUS))
        elif ROLE_LIGHTNESS in models:
            gets.append((models[ROLE_LIGHTNESS], lightness_get(), OP_LIGHTNESS_STATUS))
        answered = 0
        last_error: MeshTimeout | None = None
        for address, access, opcode in gets:
            try:
                await self._link.request(address, access, opcode)
            except MeshTimeout as err:
                last_error = err
                continue
            answered += 1
        if answered == 0 and last_error is not None:
            raise last_error
        return answered

    async def identify(self, *, currently_on: bool) -> None:
        """Health Attention if the node has it; lights blink three times; plugs refuse."""
        if ROLE_HEALTH in self._node.models:
            await self._link.send(
                self._node.models[ROLE_HEALTH], health_attention_set(ATTENTION_SECONDS)
            )
            return
        if not self._node.is_light:
            raise ValueError("identify needs Health Attention on a plug")
        address = self._address(ROLE_ONOFF)
        on = currently_on
        for _ in range(BLINKS * 2):
            on = not on
            await self._link.send(address, onoff_set(on, self._tids.next(address), ack=False))
            await asyncio.sleep(BLINK_INTERVAL)
