"""Upper and lower transport layers: access encryption, segmentation, reassembly, acks."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .crypto import ccm_decrypt, ccm_encrypt
from .errors import MalformedPDUError, MeshAuthError

MAX_UNSEGMENTED_ACCESS = 11
SEGMENT_SIZE = 12
MAX_SEGMENTS = 32
SEQ_ZERO_MASK = 0x1FFF
SEQ_MASK = 0xFFFFFF
CTL_OP_SEGMENT_ACK = 0x00

NONCE_APPLICATION = 0x01
NONCE_DEVICE = 0x02


def _upper_nonce(akf: int, szmic: int, seq: int, src: int, dst: int, iv_index: int) -> bytes:
    return (
        bytes([NONCE_APPLICATION if akf else NONCE_DEVICE, (szmic & 1) << 7])
        + seq.to_bytes(3, "big")
        + struct.pack(">HHI", src, dst, iv_index)
    )


def encrypt_upper(
    key: bytes,
    *,
    akf: int,
    szmic: int,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    access_payload: bytes,
) -> bytes:
    """Encrypt an access payload with an AppKey (akf=1) or DevKey (akf=0)."""
    nonce = _upper_nonce(akf, szmic, seq, src, dst, iv_index)
    return ccm_encrypt(key, nonce, access_payload, 8 if szmic else 4)


def decrypt_upper(
    key: bytes,
    *,
    akf: int,
    szmic: int,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    upper: bytes,
) -> bytes | None:
    """Decrypt an upper transport access PDU; None if it does not verify."""
    mic_len = 8 if szmic else 4
    if len(upper) <= mic_len:
        return None
    try:
        return ccm_decrypt(key, _upper_nonce(akf, szmic, seq, src, dst, iv_index), upper, mic_len)
    except MeshAuthError:
        return None


@dataclass(frozen=True)
class LowerAccess:
    """A parsed lower transport access PDU (one segment, or the whole unsegmented message)."""

    seg: bool
    akf: int
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    data: bytes


def unsegmented_access(upper: bytes, *, akf: int, aid: int) -> bytes:
    if len(upper) > MAX_UNSEGMENTED_ACCESS + 4:
        raise ValueError("upper transport PDU too long for an unsegmented message")
    return bytes([(akf << 6) | aid]) + upper


def segmented_access(upper: bytes, *, akf: int, aid: int, szmic: int, seq_zero: int) -> list[bytes]:
    chunks = [upper[i : i + SEGMENT_SIZE] for i in range(0, len(upper), SEGMENT_SIZE)]
    if not chunks or len(chunks) > MAX_SEGMENTS:
        raise ValueError(f"upper transport PDU needs 1-{MAX_SEGMENTS} segments")
    seg_n = len(chunks) - 1
    first = bytes([0x80 | (akf << 6) | aid])
    return [
        first
        + (
            ((szmic & 1) << 23) | ((seq_zero & SEQ_ZERO_MASK) << 10) | (seg_o << 5) | seg_n
        ).to_bytes(3, "big")
        + chunk
        for seg_o, chunk in enumerate(chunks)
    ]


def parse_lower_access(pdu: bytes) -> LowerAccess:
    if not pdu:
        raise MalformedPDUError("empty lower transport PDU")
    seg, akf, aid = pdu[0] >> 7, (pdu[0] >> 6) & 1, pdu[0] & 0x3F
    if not seg:
        if len(pdu) < 1 + 1 + 4:
            raise MalformedPDUError("unsegmented access PDU too short")
        return LowerAccess(False, akf, aid, 0, 0, 0, 0, bytes(pdu[1:]))
    if len(pdu) < 5:
        raise MalformedPDUError("segmented access PDU too short")
    header = int.from_bytes(pdu[1:4], "big")
    seg_o, seg_n = (header >> 5) & 0x1F, header & 0x1F
    if seg_o > seg_n:
        raise MalformedPDUError("segment offset beyond last segment")
    return LowerAccess(
        True, akf, aid, header >> 23, (header >> 10) & SEQ_ZERO_MASK, seg_o, seg_n, bytes(pdu[4:])
    )


def seq_auth_from_segment(net_seq: int, seq_zero: int) -> int | None:
    """Recover the 24-bit seq of a segmented message's first segment from any segment."""
    seq = net_seq - (net_seq & SEQ_ZERO_MASK) + seq_zero
    if seq > net_seq:
        seq -= SEQ_ZERO_MASK + 1
    return seq if seq >= 0 else None


@dataclass(frozen=True)
class SegmentAck:
    obo: bool
    seq_zero: int
    block_ack: int


def make_segment_ack(seq_zero: int, block_ack: int, *, obo: bool = False) -> bytes:
    """Lower transport control PDU (opcode 0x00) acknowledging received segments."""
    head = (int(obo) << 15) | ((seq_zero & SEQ_ZERO_MASK) << 2)
    return bytes([CTL_OP_SEGMENT_ACK]) + head.to_bytes(2, "big") + block_ack.to_bytes(4, "big")


def parse_segment_ack(pdu: bytes) -> SegmentAck | None:
    if len(pdu) != 7 or pdu[0] != CTL_OP_SEGMENT_ACK:
        return None
    head = int.from_bytes(pdu[1:3], "big")
    return SegmentAck(
        bool(head >> 15), (head >> 2) & SEQ_ZERO_MASK, int.from_bytes(pdu[3:7], "big")
    )
