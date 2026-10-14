"""In-process SIG Mesh simulator: a GATT proxy plus nodes, using the real mesh crypto."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from mesh.config import (
    OP_APPKEY_ADD,
    OP_APPKEY_STATUS,
    OP_COMPOSITION_GET,
    OP_COMPOSITION_STATUS,
    OP_MODEL_APP_BIND,
    OP_MODEL_APP_STATUS,
    OP_NODE_RESET,
    OP_NODE_RESET_STATUS,
    OP_PUBLICATION_SET,
    OP_PUBLICATION_STATUS,
    STATUS_SUCCESS,
    CompositionData,
    Element,
    encode_composition,
)
from mesh.models import (
    OP_CTL_GET,
    OP_CTL_SET,
    OP_CTL_SET_UNACK,
    OP_CTL_STATUS,
    OP_CTL_TEMP_GET,
    OP_CTL_TEMP_RANGE_GET,
    OP_CTL_TEMP_RANGE_STATUS,
    OP_CTL_TEMP_SET,
    OP_CTL_TEMP_SET_UNACK,
    OP_CTL_TEMP_STATUS,
    OP_HEALTH_ATTENTION_GET,
    OP_HEALTH_ATTENTION_SET,
    OP_HEALTH_ATTENTION_SET_UNACK,
    OP_HEALTH_ATTENTION_STATUS,
    OP_LIGHTNESS_GET,
    OP_LIGHTNESS_SET,
    OP_LIGHTNESS_SET_UNACK,
    OP_LIGHTNESS_STATUS,
    OP_ONOFF_GET,
    OP_ONOFF_SET,
    OP_ONOFF_SET_UNACK,
    OP_ONOFF_STATUS,
    encode_opcode,
    parse_opcode,
)

NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
CLIENT = 0x0001
SIM_PROXY = 0x7FF0
TUYA_VENDOR_MODEL = (0x07D0 << 16) | 0x0004
KINDS = ("ctl", "lightness", "onoff")


def run(coro: Coroutine[Any, Any, Any], timeout: float = 5.0) -> Any:
    """Run a test scenario on a fresh event loop, failing it if it hangs."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _model_id(raw: bytes) -> int:
    if len(raw) == 2:
        return struct.unpack("<H", raw)[0]
    cid, model = struct.unpack("<HH", raw)
    return (cid << 16) | model


@dataclass
class SimNode:
    """One simulated node: Config, Health, OnOff, Lightness, CTL and CTL Temperature servers."""

    unicast: int
    dev_key: bytes = field(repr=False)
    kind: str = "ctl"
    tuya_quirks: bool = False
    has_app_key: bool = True
    on: bool = False
    lightness: int = 0xFFFF
    kelvin: int = 4000
    attention: int = 0
    kelvin_min: int = 2700
    kelvin_max: int = 6500
    reset: bool = False
    bindings: set[tuple[int, int]] = field(default_factory=set)
    publications: dict[tuple[int, int], int] = field(default_factory=dict)
    received: list[tuple[int, int, bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown node kind {self.kind!r}")

    @property
    def element_count(self) -> int:
        return 2 if self.kind == "ctl" else 1

    @property
    def present_lightness(self) -> int:
        return self.lightness if self.on else 0

    def composition(self) -> CompositionData:
        if self.kind == "ctl":
            elements = (
                Element(0, (0x0000, 0x0002, 0x1000, 0x1300, 0x1303), (TUYA_VENDOR_MODEL,)),
                Element(0, (0x1306,), ()),
            )
        elif self.kind == "lightness":
            elements = (Element(0, (0x0000, 0x0002, 0x1000, 0x1300), ()),)
        else:
            elements = (Element(0, (0x0000, 0x1000), ()),)
        return CompositionData(0, 0x07D0, 0x0280, 0x3737, 10, 3, elements)

    def handle(self, dst: int, access: bytes, *, dev_key: bool) -> list[tuple[int, bytes]]:
        """Process one access message; returns the replies as (src, access payload)."""
        if self.reset:
            return []
        opcode, params = parse_opcode(access)
        self.received.append((dst, opcode, params))
        if dev_key:
            return self._config(opcode, params)
        return self._model(dst - self.unicast, opcode, params)

    def _config(self, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        me = self.unicast
        ok = bytes([STATUS_SUCCESS])
        if opcode == OP_COMPOSITION_GET:
            return [
                (me, encode_opcode(OP_COMPOSITION_STATUS) + encode_composition(self.composition()))
            ]
        if opcode == OP_APPKEY_ADD and len(params) == 19:
            self.has_app_key = True
            return [(me, encode_opcode(OP_APPKEY_STATUS) + ok + params[:3])]
        if opcode == OP_MODEL_APP_BIND and len(params) in (6, 8):
            element = struct.unpack_from("<H", params)[0]
            self.bindings.add((element, _model_id(params[4:])))
            return [(me, encode_opcode(OP_MODEL_APP_STATUS) + ok + params)]
        if opcode == OP_PUBLICATION_SET and len(params) in (11, 13):
            element, publish = struct.unpack_from("<HH", params)
            self.publications[(element, _model_id(params[9:]))] = publish
            return [(me, encode_opcode(OP_PUBLICATION_STATUS) + ok + params)]
        if opcode == OP_NODE_RESET:
            self.reset = True
            return [(me, encode_opcode(OP_NODE_RESET_STATUS))]
        return []

    def _reply(
        self, opcode: int, unack: int, status: int, params: bytes, src: int | None = None
    ) -> list[tuple[int, bytes]]:
        if opcode == unack:
            return []
        return [(self.unicast if src is None else src, encode_opcode(status) + params)]

    def _set_level(self, lightness: int) -> bool:
        """Apply a Lightness or CTL Set; False when a Tuya quirk ignores it."""
        if self.tuya_quirks and (not self.on or lightness == 0):
            return False
        if lightness == 0:
            self.on = False
        else:
            self.on = True
            self.lightness = lightness
        return True

    def _model(self, element: int, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        if element == 1 and self.kind == "ctl":
            return self._ctl_temperature(opcode, params)
        if element != 0:
            return []
        if opcode in (OP_ONOFF_GET, OP_ONOFF_SET, OP_ONOFF_SET_UNACK):
            if opcode != OP_ONOFF_GET:
                if not params:
                    return []
                self.on = bool(params[0])
            return self._reply(opcode, OP_ONOFF_SET_UNACK, OP_ONOFF_STATUS, bytes([int(self.on)]))
        if self.kind == "onoff":
            return []
        if opcode in (
            OP_HEALTH_ATTENTION_GET,
            OP_HEALTH_ATTENTION_SET,
            OP_HEALTH_ATTENTION_SET_UNACK,
        ):
            if opcode != OP_HEALTH_ATTENTION_GET:
                if not params:
                    return []
                self.attention = params[0]
            return self._reply(
                opcode,
                OP_HEALTH_ATTENTION_SET_UNACK,
                OP_HEALTH_ATTENTION_STATUS,
                bytes([self.attention]),
            )
        if opcode in (OP_LIGHTNESS_GET, OP_LIGHTNESS_SET, OP_LIGHTNESS_SET_UNACK):
            if opcode != OP_LIGHTNESS_GET:
                if len(params) < 3:
                    return []
                self._set_level(struct.unpack_from("<H", params)[0])
            return self._reply(
                opcode,
                OP_LIGHTNESS_SET_UNACK,
                OP_LIGHTNESS_STATUS,
                struct.pack("<H", self.present_lightness),
            )
        if self.kind != "ctl":
            return []
        if opcode in (OP_CTL_GET, OP_CTL_SET, OP_CTL_SET_UNACK):
            if opcode != OP_CTL_GET:
                if len(params) < 7:
                    return []
                lightness, kelvin = struct.unpack_from("<HH", params)
                if self._set_level(lightness):
                    self.kelvin = kelvin
            return self._reply(
                opcode,
                OP_CTL_SET_UNACK,
                OP_CTL_STATUS,
                struct.pack("<HH", self.present_lightness, self.kelvin),
            )
        if opcode == OP_CTL_TEMP_RANGE_GET:
            params = struct.pack("<BHH", STATUS_SUCCESS, self.kelvin_min, self.kelvin_max)
            return [(self.unicast, encode_opcode(OP_CTL_TEMP_RANGE_STATUS) + params)]
        return []

    def _ctl_temperature(self, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        if opcode not in (OP_CTL_TEMP_GET, OP_CTL_TEMP_SET, OP_CTL_TEMP_SET_UNACK):
            return []
        if opcode != OP_CTL_TEMP_GET:
            if len(params) < 5:
                return []
            if not (self.tuya_quirks and not self.on):
                self.kelvin = struct.unpack_from("<H", params)[0]
        return self._reply(
            opcode,
            OP_CTL_TEMP_SET_UNACK,
            OP_CTL_TEMP_STATUS,
            struct.pack("<Hh", self.kelvin, 0),
            src=self.unicast + 1,
        )
