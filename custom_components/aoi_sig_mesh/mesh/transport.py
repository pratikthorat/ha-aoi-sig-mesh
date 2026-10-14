"""Upper and lower transport layers: access encryption, segmentation, reassembly, acks."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class Reassembled:
    seq_auth: int  # 24-bit SEQ of the first segment (the upper transport nonce uses it)
    akf: int
    aid: int
    szmic: int
    upper: bytes
    iv_index: int  # IV index of the first segment; decrypt the upper PDU with it


@dataclass(frozen=True)
class SegmentResult:
    message: Reassembled | None
    block_ack: int
    seq_zero: int
    send_ack: bool


@dataclass
class _Partial:
    key: int  # (iv_index << 24) | seq_auth: the full SeqAuth, used for ordering
    seq_auth: int
    iv_index: int
    seg_n: int
    akf: int
    aid: int
    szmic: int
    parts: dict[int, bytes] = field(default_factory=dict)

    @property
    def block_ack(self) -> int:
        return sum(1 << seg_o for seg_o in self.parts)


class SegmentReassembler:
    """Per-source segment reassembly with duplicate re-acks and stale-segment rejection."""

    def __init__(self) -> None:
        self._partial: dict[int, _Partial] = {}
        self._done: dict[int, tuple[int, int]] = {}  # src -> (SeqAuth key, block_ack)

    def forget(self, first: int, count: int) -> None:
        """Drop segment state for a node's elements, e.g. after it is reset and re-provisioned."""
        for src in range(first, first + count):
            self._partial.pop(src, None)
            self._done.pop(src, None)

    def add(self, src: int, net_seq: int, seg: LowerAccess, *, iv_index: int) -> SegmentResult:
        ignore = SegmentResult(None, 0, seg.seq_zero, False)
        if not seg.seg or seg.seg_o > seg.seg_n:
            return ignore
        if len(seg.data) > SEGMENT_SIZE or (
            seg.seg_o < seg.seg_n and len(seg.data) != SEGMENT_SIZE
        ):
            return ignore
        seq_auth = seq_auth_from_segment(net_seq, seg.seq_zero)
        if seq_auth is None:
            return ignore
        key = (iv_index << 24) | seq_auth
        done = self._done.get(src)
        if done is not None:
            if key == done[0]:
                return SegmentResult(None, done[1], seg.seq_zero, True)
            if key < done[0]:
                return ignore
        partial = self._partial.get(src)
        if partial is not None and key < partial.key:
            return ignore
        if partial is None or key > partial.key:
            # Every check has passed, so the newer message may replace the one in progress.
            partial = _Partial(key, seq_auth, iv_index, seg.seg_n, seg.akf, seg.aid, seg.szmic)
            self._partial[src] = partial
        elif (seg.seg_n, seg.akf, seg.aid, seg.szmic) != (
            partial.seg_n,
            partial.akf,
            partial.aid,
            partial.szmic,
        ):
            return ignore
        partial.parts[seg.seg_o] = seg.data
        if len(partial.parts) <= partial.seg_n:
            return SegmentResult(None, partial.block_ack, seg.seq_zero, False)
        upper = b"".join(partial.parts[i] for i in range(partial.seg_n + 1))
        block_ack = partial.block_ack
        del self._partial[src]
        self._done[src] = (key, block_ack)
        message = Reassembled(
            partial.seq_auth, partial.akf, partial.aid, partial.szmic, upper, partial.iv_index
        )
        return SegmentResult(message, block_ack, seg.seq_zero, True)
