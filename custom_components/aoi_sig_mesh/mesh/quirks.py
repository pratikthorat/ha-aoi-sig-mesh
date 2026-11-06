"""Vendor behaviour flags, keyed by (CID, PID). A CID-only entry (PID None) is a wildcard."""

from __future__ import annotations

import enum


class Quirk(enum.Flag):
    NONE = 0
    OFF_VIA_ONOFF = enum.auto()  # turn off with Generic OnOff OFF instead of Lightness 0
    ON_BEFORE_SET = enum.auto()  # a Lightness/CTL Set while off is ignored: send OnOff ON first
    IGNORE_VENDOR_OPCODES = enum.auto()  # drop vendor messages (Tuya time requests) quietly


CID_TUYA = 0x07D0

# The tested AiDot solar spotlights (SL5C) have a quirk:
#   They will 'dim' the light rather than turn it off under normal operation.
#   The only way to turn them off is to send a Generic OnOff OFF message.
#   They also seem to ignore Lightness Set messages when the light is off
#   so we must send an OnOff ON first before setting the lightness.
CID_AIDOT = 0x0504
AIDOT_SOLAR_SPOTLIGHTS_SL5C = (CID_AIDOT, 0x6212)

QUIRKS: dict[tuple[int, int | None], Quirk] = {
    (CID_TUYA, None): Quirk.OFF_VIA_ONOFF | Quirk.ON_BEFORE_SET | Quirk.IGNORE_VENDOR_OPCODES,
    AIDOT_SOLAR_SPOTLIGHTS_SL5C: Quirk.OFF_VIA_ONOFF,
}

VENDOR_OPCODE_MIN = 0xC00000


def quirks_for(cid: int | None, pid: int | None) -> Quirk:
    """Flags for a node: an exact (CID, PID) entry wins over the CID wildcard."""
    if cid is None:
        return Quirk.NONE
    if pid is not None and (cid, pid) in QUIRKS:
        return QUIRKS[(cid, pid)]
    return QUIRKS.get((cid, None), Quirk.NONE)


def is_vendor_opcode(opcode: int) -> bool:
    """3-byte opcodes belong to vendor models."""
    return opcode >= VENDOR_OPCODE_MIN
