"""Model IDs, opcodes and Generic/Light/Health model messages (Mesh Model spec)."""

from __future__ import annotations

import struct

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
