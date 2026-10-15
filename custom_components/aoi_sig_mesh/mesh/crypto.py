"""Mesh Profile cryptographic primitives (spec section 3.8.2)."""

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

from .errors import MeshAuthError, MeshCryptoError

KEY_LEN = 16


def _check_key(key: bytes) -> None:
    if len(key) != KEY_LEN:
        raise MeshCryptoError(f"key must be {KEY_LEN} bytes, got {len(key)}")


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """Encrypt one 16-byte block with AES-128."""
    _check_key(key)
    if len(block) != 16:
        raise MeshCryptoError(f"block must be 16 bytes, got {len(block)}")
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """AES-CMAC (RFC 4493)."""
    _check_key(key)
    cmac = CMAC(algorithms.AES(key))
    cmac.update(message)
    return cmac.finalize()


ZERO_KEY = bytes(16)


def s1(m: bytes | str) -> bytes:
    """Salt generation function s1."""
    if isinstance(m, str):
        m = m.encode("ascii")
    return aes_cmac(ZERO_KEY, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Derivation function k1."""
    t = aes_cmac(salt, n)
    return aes_cmac(t, p)


def k2(n: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    """Network key material: (NID, EncryptionKey, PrivacyKey)."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(n: bytes) -> bytes:
    """Network ID (8 bytes)."""
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[8:]


def k4(n: bytes) -> int:
    """Application key identifier (AID, 6 bits)."""
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F


def beacon_key(net_key: bytes) -> bytes:
    """BeaconKey used to authenticate Secure Network Beacons."""
    return k1(net_key, s1(b"nkbk"), b"id128\x01")


def identity_key(net_key: bytes) -> bytes:
    """IdentityKey used to hash Node Identity adverts."""
    return k1(net_key, s1(b"nkik"), b"id128\x01")


NONCE_LEN = 13


def _check_ccm(key: bytes, nonce: bytes, mic_len: int) -> None:
    _check_key(key)
    if len(nonce) != NONCE_LEN:
        raise MeshCryptoError(f"nonce must be {NONCE_LEN} bytes, got {len(nonce)}")
    if mic_len not in (4, 8):
        raise MeshCryptoError(f"MIC length must be 4 or 8, got {mic_len}")


def ccm_encrypt(
    key: bytes, nonce: bytes, plaintext: bytes, mic_len: int, aad: bytes = b""
) -> bytes:
    """AES-CCM encrypt; returns ciphertext followed by the MIC."""
    _check_ccm(key, nonce, mic_len)
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad or None)


def ccm_decrypt(key: bytes, nonce: bytes, data: bytes, mic_len: int, aad: bytes = b"") -> bytes:
    """AES-CCM decrypt ciphertext+MIC; raises MeshAuthError if the MIC fails."""
    _check_ccm(key, nonce, mic_len)
    if len(data) <= mic_len:
        raise MeshAuthError("data shorter than its MIC")
    try:
        return AESCCM(key, tag_length=mic_len).decrypt(nonce, data, aad or None)
    except (InvalidTag, ValueError) as err:
        raise MeshAuthError("MIC check failed") from err
