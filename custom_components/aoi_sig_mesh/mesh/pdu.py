"""Network layer, proxy PDU SAR, secure network beacons and proxy configuration."""

from __future__ import annotations

import hmac
import struct
from dataclasses import dataclass

from .crypto import aes_cmac, aes_ecb, beacon_key, ccm_decrypt, ccm_encrypt, k2, k3
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
MAX_IV_INDEX = 0xFFFFFFFF


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
    if len(pdu) < MIN_NETWORK_PDU or not 0 <= iv_index <= MAX_IV_INDEX:
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


def proxy_pdus(pdu_type: int, payload: bytes, max_write: int) -> list[bytes]:
    """Split a proxy message into GATT writes of at most ``max_write`` bytes."""
    if max_write < 2:
        raise ValueError("max_write must be at least 2")
    chunk = max_write - 1
    if len(payload) <= chunk:
        return [bytes([(SAR_COMPLETE << 6) | pdu_type]) + payload]
    pieces = [payload[i : i + chunk] for i in range(0, len(payload), chunk)]
    parts = []
    for index, piece in enumerate(pieces):
        if index == 0:
            sar = SAR_FIRST
        elif index == len(pieces) - 1:
            sar = SAR_LAST
        else:
            sar = SAR_CONTINUE
        parts.append(bytes([(sar << 6) | pdu_type]) + piece)
    return parts


class ProxyReassembler:
    """Rebuild proxy messages from GATT notifications. Never raises on junk."""

    def __init__(self) -> None:
        self._type: int | None = None
        self._buffer = bytearray()

    def _reset(self) -> None:
        self._type = None
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[int, bytes] | None:
        if not data:
            return None
        sar, pdu_type, body = data[0] >> 6, data[0] & 0x3F, data[1:]
        if sar == SAR_COMPLETE:
            self._reset()
            return pdu_type, bytes(body)
        if sar == SAR_FIRST:
            self._type = pdu_type
            self._buffer = bytearray(body)
            return None
        if self._type is None or pdu_type != self._type:
            self._reset()
            return None
        self._buffer += body
        if len(self._buffer) > MAX_PROXY_MESSAGE:
            self._reset()
            return None
        if sar == SAR_LAST:
            message = bytes(self._buffer)
            self._reset()
            return pdu_type, message
        return None


BEACON_SECURE = 0x01
BEACON_LEN = 22
FLAG_KEY_REFRESH = 0x01
FLAG_IV_UPDATE = 0x02


@dataclass(frozen=True)
class SecureBeacon:
    """An authenticated Secure Network Beacon."""

    key_refresh: bool
    iv_update: bool
    iv_index: int


def _beacon_auth(keys: NetworkKeys, flags: int, iv_index: int) -> bytes:
    return aes_cmac(
        keys.beacon_key, bytes([flags]) + keys.network_id + iv_index.to_bytes(4, "big")
    )[:8]


def make_secure_beacon(
    keys: NetworkKeys, iv_index: int, *, iv_update: bool = False, key_refresh: bool = False
) -> bytes:
    """Build a Secure Network Beacon (the payload of a proxy beacon PDU)."""
    flags = (FLAG_KEY_REFRESH if key_refresh else 0) | (FLAG_IV_UPDATE if iv_update else 0)
    return (
        bytes([BEACON_SECURE, flags])
        + keys.network_id
        + iv_index.to_bytes(4, "big")
        + _beacon_auth(keys, flags, iv_index)
    )


def parse_secure_beacon(keys: NetworkKeys, data: bytes) -> SecureBeacon | None:
    """Verify a Secure Network Beacon for our network; None if it is not valid."""
    if len(data) != BEACON_LEN or data[0] != BEACON_SECURE:
        return None
    flags = data[1]
    if data[2:10] != keys.network_id:
        return None
    iv_index = int.from_bytes(data[10:14], "big")
    if not hmac.compare_digest(data[14:22], _beacon_auth(keys, flags, iv_index)):
        return None
    return SecureBeacon(bool(flags & FLAG_KEY_REFRESH), bool(flags & FLAG_IV_UPDATE), iv_index)


FILTER_ACCEPT = 0x00
FILTER_REJECT = 0x01
PROXY_OP_SET_FILTER_TYPE = 0x00
PROXY_OP_ADD_ADDRESSES = 0x01
PROXY_OP_FILTER_STATUS = 0x03


def encrypt_proxy_config(
    keys: NetworkKeys, *, seq: int, src: int, iv_index: int, message: bytes
) -> bytes:
    """Encrypt a proxy configuration message (proxy nonce, CTL=1, TTL=0, DST=0)."""
    return encrypt_network_pdu(
        keys,
        ctl=1,
        ttl=0,
        seq=seq,
        src=src,
        dst=0x0000,
        transport_pdu=message,
        iv_index=iv_index,
        nonce_type=NONCE_PROXY,
    )


def decrypt_proxy_config(keys: NetworkKeys, pdu: bytes, iv_index: int) -> NetworkPDU | None:
    """Decrypt a proxy configuration message; None if it does not verify."""
    return decrypt_network_pdu(keys, pdu, iv_index, nonce_type=NONCE_PROXY)


def set_filter_type(filter_type: int) -> bytes:
    return bytes([PROXY_OP_SET_FILTER_TYPE, filter_type])


def add_addresses(*addresses: int) -> bytes:
    return bytes([PROXY_OP_ADD_ADDRESSES]) + b"".join(a.to_bytes(2, "big") for a in addresses)


@dataclass(frozen=True)
class FilterStatus:
    filter_type: int
    list_size: int


def parse_filter_status(message: bytes) -> FilterStatus | None:
    if len(message) != 4 or message[0] != PROXY_OP_FILTER_STATUS:
        return None
    return FilterStatus(message[1], int.from_bytes(message[2:4], "big"))
