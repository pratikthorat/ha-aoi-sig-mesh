"""PB-GATT provisioning with No OOB authentication (Mesh Profile section 5.4)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric import ec

from .crypto import aes_cmac, ccm_decrypt, ccm_encrypt, k1, s1
from .errors import MalformedPDUError

# Provisioning PDU types (Mesh Profile 5.4.1)
PROV_INVITE = 0x00
PROV_CAPABILITIES = 0x01
PROV_START = 0x02
PROV_PUBLIC_KEY = 0x03
PROV_CONFIRMATION = 0x05
PROV_RANDOM = 0x06
PROV_DATA = 0x07
PROV_COMPLETE = 0x08
PROV_FAILED = 0x09

# Provisioning Failed error codes (Mesh Profile 5.4.1.10)
FAILED_INVALID_PDU = 0x01
FAILED_INVALID_FORMAT = 0x02
FAILED_UNEXPECTED_PDU = 0x03
FAILED_CONFIRMATION = 0x04
FAILED_OUT_OF_RESOURCES = 0x05
FAILED_DECRYPTION = 0x06
FAILED_UNEXPECTED_ERROR = 0x07
FAILED_CANNOT_ASSIGN_ADDRESSES = 0x08
FAILED_REASONS = {
    FAILED_INVALID_PDU: "invalid PDU",
    FAILED_INVALID_FORMAT: "invalid format",
    FAILED_UNEXPECTED_PDU: "unexpected PDU",
    FAILED_CONFIRMATION: "confirmation failed",
    FAILED_OUT_OF_RESOURCES: "out of resources",
    FAILED_DECRYPTION: "decryption failed",
    FAILED_UNEXPECTED_ERROR: "unexpected error",
    FAILED_CANNOT_ASSIGN_ADDRESSES: "cannot assign addresses",
}

ALGORITHM_FIPS_P256 = 0x0001  # Capabilities Algorithms bit 0
STATIC_OOB_AVAILABLE = 0x01
STATIC_OOB_ONLY = 0x02  # Mesh Protocol 1.1: "only OOB authenticated provisioning supported"
FLAG_KEY_REFRESH = 0x01
FLAG_IV_UPDATE = 0x02

CAPABILITIES = struct.Struct(">BHBBBHBH")  # elements, algorithms, pk type, static, out, in
PROVISIONING_DATA = struct.Struct(">16sHBIH")  # NetKey, index, flags, IV index, unicast
PDU_LENGTHS = {
    PROV_INVITE: 1,
    PROV_CAPABILITIES: CAPABILITIES.size,
    PROV_START: 5,
    PROV_PUBLIC_KEY: 64,
    PROV_CONFIRMATION: 16,
    PROV_RANDOM: 16,
    PROV_DATA: PROVISIONING_DATA.size + 8,
    PROV_COMPLETE: 0,
    PROV_FAILED: 1,
}
START_NO_OOB = bytes(5)  # FIPS P-256, no OOB public key, No OOB authentication
NO_OOB_AUTH = bytes(16)  # AuthValue for No OOB


@dataclass(frozen=True)
class Capabilities:
    """A Provisioning Capabilities PDU."""

    elements: int
    algorithms: int
    public_key_type: int
    static_oob_type: int
    output_oob_size: int
    output_oob_action: int
    input_oob_size: int
    input_oob_action: int

    def encode(self) -> bytes:
        return CAPABILITIES.pack(
            self.elements,
            self.algorithms,
            self.public_key_type,
            self.static_oob_type,
            self.output_oob_size,
            self.output_oob_action,
            self.input_oob_size,
            self.input_oob_action,
        )


def parse_capabilities(params: bytes) -> Capabilities:
    if len(params) != CAPABILITIES.size:
        raise MalformedPDUError(f"Capabilities must be {CAPABILITIES.size} bytes")
    return Capabilities(*CAPABILITIES.unpack(params))


@dataclass(frozen=True)
class ProvisioningData:
    """What the provisioner hands the device: network key, IV index and its address."""

    net_key: bytes = field(repr=False)
    net_key_index: int
    flags: int
    iv_index: int
    unicast: int

    def encode(self) -> bytes:
        return PROVISIONING_DATA.pack(
            self.net_key, self.net_key_index, self.flags, self.iv_index, self.unicast
        )


def parse_provisioning_data(data: bytes) -> ProvisioningData:
    if len(data) != PROVISIONING_DATA.size:
        raise MalformedPDUError(f"Provisioning Data must be {PROVISIONING_DATA.size} bytes")
    return ProvisioningData(*PROVISIONING_DATA.unpack(data))


def prov_pdu(pdu_type: int, params: bytes = b"") -> bytes:
    """One provisioning PDU: the type octet, then the parameters."""
    return bytes([pdu_type]) + params


def parse_prov_pdu(pdu: bytes) -> tuple[int, bytes]:
    """(type, parameters); MalformedPDUError for an unknown type or a wrong length."""
    if not pdu:
        raise MalformedPDUError("empty provisioning PDU")
    pdu_type, params = pdu[0] & 0x3F, pdu[1:]
    if pdu[0] & 0xC0 or pdu_type not in PDU_LENGTHS:
        raise MalformedPDUError(f"unknown provisioning PDU 0x{pdu[0]:02X}")
    if len(params) != PDU_LENGTHS[pdu_type]:
        raise MalformedPDUError(f"provisioning PDU 0x{pdu_type:02X} has a bad length")
    return pdu_type, params


# keys


def private_key_from_bytes(raw: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def generate_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_key_bytes(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    """The public key as sent on the air: X then Y, 32 bytes each, big-endian."""
    numbers = private_key.public_key().public_numbers()
    return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def ecdh_secret(private_key: ec.EllipticCurvePrivateKey, peer: bytes) -> bytes:
    """The P-256 ECDH shared secret; MalformedPDUError if ``peer`` is not a point on the curve."""
    if len(peer) != 64:
        raise MalformedPDUError("a public key is 64 bytes")
    try:
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(peer[:32], "big"), int.from_bytes(peer[32:], "big"), ec.SECP256R1()
        ).public_key()
        return private_key.exchange(ec.ECDH(), public)
    except ValueError as err:
        raise MalformedPDUError("the public key is not a valid P-256 point") from err


# confirmation and session keys (Mesh Profile 5.4.2.4 and 5.4.2.5)


def confirmation_inputs(
    invite: bytes, capabilities: bytes, start: bytes, public_p: bytes, public_d: bytes
) -> bytes:
    return invite + capabilities + start + public_p + public_d


def confirmation_salt(inputs: bytes) -> bytes:
    return s1(inputs)


def confirmation_key(secret: bytes, salt: bytes) -> bytes:
    return k1(secret, salt, b"prck")


def confirmation(key: bytes, random: bytes, auth: bytes = NO_OOB_AUTH) -> bytes:
    return aes_cmac(key, random + auth)


def provisioning_salt(conf_salt: bytes, random_p: bytes, random_d: bytes) -> bytes:
    return s1(conf_salt + random_p + random_d)


def session_key(secret: bytes, prov_salt: bytes) -> bytes:
    return k1(secret, prov_salt, b"prsk")


def session_nonce(secret: bytes, prov_salt: bytes) -> bytes:
    return k1(secret, prov_salt, b"prsn")[3:]


def device_key(secret: bytes, prov_salt: bytes) -> bytes:
    return k1(secret, prov_salt, b"prdk")


def encrypt_provisioning_data(key: bytes, nonce: bytes, data: ProvisioningData) -> bytes:
    return ccm_encrypt(key, nonce, data.encode(), 8)


def decrypt_provisioning_data(key: bytes, nonce: bytes, encrypted: bytes) -> ProvisioningData:
    """Raises MeshAuthError when the MIC fails, MalformedPDUError for a bad length."""
    return parse_provisioning_data(ccm_decrypt(key, nonce, encrypted, 8))
