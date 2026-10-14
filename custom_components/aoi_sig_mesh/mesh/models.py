"""Model IDs, opcodes and Generic/Light/Health model messages (Mesh Model spec)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import MalformedPDUError

MODEL_CONFIG_SERVER = 0x0000
MODEL_HEALTH_SERVER = 0x0002
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_CTL_TEMP_SERVER = 0x1306

OP_HEALTH_ATTENTION_GET = 0x8004
OP_HEALTH_ATTENTION_SET = 0x8005
OP_HEALTH_ATTENTION_SET_UNACK = 0x8006
OP_HEALTH_ATTENTION_STATUS = 0x8007

OP_ONOFF_GET = 0x8201
OP_ONOFF_SET = 0x8202
OP_ONOFF_SET_UNACK = 0x8203
OP_ONOFF_STATUS = 0x8204

OP_LIGHTNESS_GET = 0x824B
OP_LIGHTNESS_SET = 0x824C
OP_LIGHTNESS_SET_UNACK = 0x824D
OP_LIGHTNESS_STATUS = 0x824E

OP_CTL_GET = 0x825D
OP_CTL_SET = 0x825E
OP_CTL_SET_UNACK = 0x825F
OP_CTL_STATUS = 0x8260
OP_CTL_TEMP_GET = 0x8261
OP_CTL_TEMP_RANGE_GET = 0x8262
OP_CTL_TEMP_RANGE_STATUS = 0x8263
OP_CTL_TEMP_SET = 0x8264
OP_CTL_TEMP_SET_UNACK = 0x8265
OP_CTL_TEMP_STATUS = 0x8266

KELVIN_MIN = 800
KELVIN_MAX = 20000
LIGHTNESS_MAX = 0xFFFF


def encode_opcode(opcode: int) -> bytes:
    """Encode a 1-, 2- or 3-byte opcode (0x7F is reserved)."""
    if 0 <= opcode < 0x7F:
        return bytes([opcode])
    if 0x8000 <= opcode <= 0xBFFF:
        return opcode.to_bytes(2, "big")
    if 0xC00000 <= opcode <= 0xFFFFFF:
        return opcode.to_bytes(3, "big")
    raise ValueError(f"invalid opcode 0x{opcode:X}")


def parse_opcode(access: bytes) -> tuple[int, bytes]:
    """Split an access payload into (opcode, params)."""
    if not access:
        raise MalformedPDUError("empty access payload")
    first = access[0]
    if first == 0x7F:
        raise MalformedPDUError("reserved opcode 0x7F")
    size = 1 if first < 0x80 else 2 if first < 0xC0 else 3
    if len(access) < size:
        raise MalformedPDUError("truncated opcode")
    return int.from_bytes(access[:size], "big"), bytes(access[size:])


_STEP_MS = (100, 1_000, 10_000, 600_000)
_MAX_STEPS = 0x3E
_UNKNOWN_STEPS = 0x3F


def encode_transition(ms: int) -> int:
    """Encode a duration as the Generic Default Transition Time byte (rounding up)."""
    if ms <= 0:
        return 0
    for resolution, step_ms in enumerate(_STEP_MS):
        steps = -(-ms // step_ms)
        if steps <= _MAX_STEPS:
            return (resolution << 6) | steps
    return (3 << 6) | _MAX_STEPS


def decode_transition(value: int) -> int | None:
    """Decode a transition-time byte to milliseconds; None when the value is 'unknown'."""
    steps = value & 0x3F
    if steps == _UNKNOWN_STEPS:
        return None
    return steps * _STEP_MS[value >> 6]


def _tail(transition_ms: int | None, delay_ms: int) -> bytes:
    if transition_ms is None:
        return b""
    return bytes([encode_transition(transition_ms), min(max(delay_ms, 0) // 5, 0xFF)])


def _check_lightness(lightness: int) -> None:
    if not 0 <= lightness <= LIGHTNESS_MAX:
        raise ValueError(f"lightness {lightness} out of range 0-{LIGHTNESS_MAX}")


def _check_kelvin(kelvin: int) -> None:
    if not KELVIN_MIN <= kelvin <= KELVIN_MAX:
        raise ValueError(f"temperature {kelvin} K out of range {KELVIN_MIN}-{KELVIN_MAX}")


def onoff_get() -> bytes:
    return encode_opcode(OP_ONOFF_GET)


def onoff_set(
    on: bool, tid: int, *, transition_ms: int | None = None, delay_ms: int = 0, ack: bool = True
) -> bytes:
    opcode = OP_ONOFF_SET if ack else OP_ONOFF_SET_UNACK
    return (
        encode_opcode(opcode) + bytes([1 if on else 0, tid & 0xFF]) + _tail(transition_ms, delay_ms)
    )


def lightness_get() -> bytes:
    return encode_opcode(OP_LIGHTNESS_GET)


def lightness_set(
    lightness: int,
    tid: int,
    *,
    transition_ms: int | None = None,
    delay_ms: int = 0,
    ack: bool = True,
) -> bytes:
    _check_lightness(lightness)
    opcode = OP_LIGHTNESS_SET if ack else OP_LIGHTNESS_SET_UNACK
    return (
        encode_opcode(opcode)
        + struct.pack("<HB", lightness, tid & 0xFF)
        + _tail(transition_ms, delay_ms)
    )


def ctl_get() -> bytes:
    return encode_opcode(OP_CTL_GET)


def ctl_set(
    lightness: int,
    kelvin: int,
    tid: int,
    *,
    delta_uv: int = 0,
    transition_ms: int | None = None,
    delay_ms: int = 0,
    ack: bool = True,
) -> bytes:
    _check_lightness(lightness)
    _check_kelvin(kelvin)
    opcode = OP_CTL_SET if ack else OP_CTL_SET_UNACK
    return (
        encode_opcode(opcode)
        + struct.pack("<HHhB", lightness, kelvin, delta_uv, tid & 0xFF)
        + _tail(transition_ms, delay_ms)
    )


def ctl_temperature_get() -> bytes:
    return encode_opcode(OP_CTL_TEMP_GET)


def ctl_temperature_set(
    kelvin: int,
    tid: int,
    *,
    delta_uv: int = 0,
    transition_ms: int | None = None,
    delay_ms: int = 0,
    ack: bool = True,
) -> bytes:
    _check_kelvin(kelvin)
    opcode = OP_CTL_TEMP_SET if ack else OP_CTL_TEMP_SET_UNACK
    return (
        encode_opcode(opcode)
        + struct.pack("<HhB", kelvin, delta_uv, tid & 0xFF)
        + _tail(transition_ms, delay_ms)
    )


def ctl_temperature_range_get() -> bytes:
    return encode_opcode(OP_CTL_TEMP_RANGE_GET)


def health_attention_set(seconds: int, *, ack: bool = False) -> bytes:
    opcode = OP_HEALTH_ATTENTION_SET if ack else OP_HEALTH_ATTENTION_SET_UNACK
    return encode_opcode(opcode) + bytes([min(max(seconds, 0), 0xFF)])


@dataclass(frozen=True)
class OnOffStatus:
    present: bool
    target: bool | None
    remaining_ms: int | None


@dataclass(frozen=True)
class LightnessStatus:
    present: int
    target: int | None
    remaining_ms: int | None


@dataclass(frozen=True)
class CTLStatus:
    present_lightness: int
    present_kelvin: int
    target_lightness: int | None
    target_kelvin: int | None
    remaining_ms: int | None


@dataclass(frozen=True)
class CTLTemperatureStatus:
    present_kelvin: int
    present_delta_uv: int
    target_kelvin: int | None
    target_delta_uv: int | None
    remaining_ms: int | None


@dataclass(frozen=True)
class CTLTemperatureRangeStatus:
    status_code: int
    min_kelvin: int
    max_kelvin: int


def _bad_length(name: str, params: bytes) -> MalformedPDUError:
    return MalformedPDUError(f"{name} with {len(params)} parameter bytes")


def parse_onoff_status(params: bytes) -> OnOffStatus:
    if len(params) == 1:
        return OnOffStatus(bool(params[0]), None, None)
    if len(params) == 3:
        return OnOffStatus(bool(params[0]), bool(params[1]), decode_transition(params[2]))
    raise _bad_length("Generic OnOff Status", params)


def parse_lightness_status(params: bytes) -> LightnessStatus:
    if len(params) == 2:
        return LightnessStatus(struct.unpack("<H", params)[0], None, None)
    if len(params) == 5:
        present, target, remaining = struct.unpack("<HHB", params)
        return LightnessStatus(present, target, decode_transition(remaining))
    raise _bad_length("Light Lightness Status", params)


def parse_ctl_status(params: bytes) -> CTLStatus:
    if len(params) == 4:
        lightness, kelvin = struct.unpack("<HH", params)
        return CTLStatus(lightness, kelvin, None, None, None)
    if len(params) == 9:
        lightness, kelvin, t_lightness, t_kelvin, remaining = struct.unpack("<HHHHB", params)
        return CTLStatus(lightness, kelvin, t_lightness, t_kelvin, decode_transition(remaining))
    raise _bad_length("Light CTL Status", params)


def parse_ctl_temperature_status(params: bytes) -> CTLTemperatureStatus:
    if len(params) == 4:
        kelvin, delta_uv = struct.unpack("<Hh", params)
        return CTLTemperatureStatus(kelvin, delta_uv, None, None, None)
    if len(params) == 9:
        kelvin, delta_uv, t_kelvin, t_delta_uv, remaining = struct.unpack("<HhHhB", params)
        return CTLTemperatureStatus(
            kelvin, delta_uv, t_kelvin, t_delta_uv, decode_transition(remaining)
        )
    raise _bad_length("Light CTL Temperature Status", params)


def parse_ctl_temperature_range_status(params: bytes) -> CTLTemperatureRangeStatus:
    if len(params) != 5:
        raise _bad_length("Light CTL Temperature Range Status", params)
    return CTLTemperatureRangeStatus(*struct.unpack("<BHH", params))


def parse_attention_status(params: bytes) -> int:
    if len(params) != 1:
        raise _bad_length("Health Attention Status", params)
    return params[0]
