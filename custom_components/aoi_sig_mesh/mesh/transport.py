"""Upper and lower transport layers: access encryption, segmentation, reassembly, acks."""

from __future__ import annotations

import struct

from .crypto import ccm_decrypt, ccm_encrypt
from .errors import MeshAuthError

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
