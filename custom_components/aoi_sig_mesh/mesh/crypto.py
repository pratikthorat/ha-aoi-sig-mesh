"""Mesh Profile cryptographic primitives (spec section 3.8.2)."""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

from .errors import MeshCryptoError

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
