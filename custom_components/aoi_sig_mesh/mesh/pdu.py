"""Network layer, proxy PDU SAR, secure network beacons and proxy configuration."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .crypto import aes_ecb, beacon_key, ccm_decrypt, ccm_encrypt, k2, k3
from .errors import MeshAuthError

PDU_NETWORK = 0
PDU_BEACON = 1
PDU_PROXY_CONFIG = 2
PDU_PROVISIONING = 3

SAR_COMPLETE = 0
SAR_FIRST = 1
SAR_CONTINUE = 2
SAR_LAST = 3

NONCE_NETWORK = 0x00
NONCE_PROXY = 0x03

MIN_NETWORK_PDU = 14  # IVI/NID + 6 obfuscated + DST + 1 transport byte + 32-bit MIC
MAX_PROXY_MESSAGE = 512


@dataclass(frozen=True, repr=False)
class NetworkKeys:
    """Key material derived from one NetKey."""

    net_key: bytes
    nid: int
    enc_key: bytes
    priv_key: bytes
    network_id: bytes
    beacon_key: bytes

    @classmethod
    def from_net_key(cls, net_key: bytes) -> NetworkKeys:
        nid, enc_key, priv_key = k2(net_key)
        return cls(net_key, nid, enc_key, priv_key, k3(net_key), beacon_key(net_key))

    def __repr__(self) -> str:
        return f"NetworkKeys(nid=0x{self.nid:02X})"


@dataclass(frozen=True)
class NetworkPDU:
    """A decrypted network PDU."""

    ivi: int
    nid: int
    ctl: int
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


def _nonce(nonce_type: int, ctl: int, ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    second = ((ctl << 7) | ttl) if nonce_type == NONCE_NETWORK else 0x00
    return (
        bytes([nonce_type, second])
        + seq.to_bytes(3, "big")
        + struct.pack(">HHI", src, 0x0000, iv_index)
    )


def _pecb(keys: NetworkKeys, iv_index: int, privacy_random: bytes) -> bytes:
    return aes_ecb(keys.priv_key, bytes(5) + iv_index.to_bytes(4, "big") + privacy_random)


def encrypt_network_pdu(
    keys: NetworkKeys,
    *,
    ctl: int,
    ttl: int,
    seq: int,
    src: int,
    dst: int,
    transport_pdu: bytes,
    iv_index: int,
    nonce_type: int = NONCE_NETWORK,
) -> bytes:
    """Encrypt and obfuscate a network PDU."""
    mic_len = 8 if ctl else 4
    nonce = _nonce(nonce_type, ctl, ttl, seq, src, iv_index)
    encrypted = ccm_encrypt(keys.enc_key, nonce, dst.to_bytes(2, "big") + transport_pdu, mic_len)
    header = bytes([(ctl << 7) | ttl]) + seq.to_bytes(3, "big") + src.to_bytes(2, "big")
    pecb = _pecb(keys, iv_index, encrypted[:7])
    obfuscated = bytes(a ^ b for a, b in zip(header, pecb[:6], strict=True))
    return bytes([((iv_index & 1) << 7) | keys.nid]) + obfuscated + encrypted


def decrypt_network_pdu(
    keys: NetworkKeys, pdu: bytes, iv_index: int, *, nonce_type: int = NONCE_NETWORK
) -> NetworkPDU | None:
    """Deobfuscate and decrypt a network PDU; None if it is not ours or does not verify."""
    if len(pdu) < MIN_NETWORK_PDU:
        return None
    ivi, nid = pdu[0] >> 7, pdu[0] & 0x7F
    if nid != keys.nid or ivi != (iv_index & 1):
        return None
    encrypted = pdu[7:]
    pecb = _pecb(keys, iv_index, encrypted[:7])
    header = bytes(a ^ b for a, b in zip(pdu[1:7], pecb[:6], strict=True))
    ctl, ttl = header[0] >> 7, header[0] & 0x7F
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")
    mic_len = 8 if ctl else 4
    if len(encrypted) < 2 + 1 + mic_len:
        return None
    try:
        plain = ccm_decrypt(
            keys.enc_key, _nonce(nonce_type, ctl, ttl, seq, src, iv_index), encrypted, mic_len
        )
    except MeshAuthError:
        return None
    return NetworkPDU(ivi, nid, ctl, ttl, seq, src, int.from_bytes(plain[:2], "big"), plain[2:])
