"""PB-GATT provisioning with No OOB authentication (Mesh Profile section 5.4)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric import ec

from .crypto import aes_cmac, ccm_decrypt, ccm_encrypt, k1, s1
from .errors import MalformedPDUError, MeshError, ProvisioningError
from .pdu import PDU_PROVISIONING, ProxyReassembler, proxy_pdus
from .proxy import GattTransport

_LOGGER = logging.getLogger(__name__)

# Read at call time, so tests can shorten them with monkeypatch.
CAPABILITIES_TIMEOUT = 10.0
PUBLIC_KEY_TIMEOUT = 15.0
CONFIRMATION_TIMEOUT = 10.0
RANDOM_TIMEOUT = 10.0
COMPLETE_TIMEOUT = 15.0
START_DELAY = 0.5  # some devices drop a Public Key sent right after Start
ATTENTION_SECONDS = 5  # the device blinks or beeps while it is being provisioned
CLOSE_TIMEOUT = 5.0

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


# the provisioner


class ProvisioningRefused(ProvisioningError):
    """The device answered with Provisioning Failed: it is not provisioned."""

    def __init__(self, step: str, code: int) -> None:
        MeshError.__init__(self, step, code)  # both in args, so the error pickles
        self.step = step
        self.code = code
        self.message = f"the device refused: {FAILED_REASONS.get(code, f'error 0x{code:02X}')}"


class OobRequired(ProvisioningError):
    """The device accepts only OOB-authenticated provisioning; we provision with No OOB."""


@dataclass(frozen=True)
class ProvisioningResult:
    unicast: int
    element_count: int
    dev_key: bytes = field(repr=False)
    capabilities: Capabilities


class Provisioner:
    """Runs the provisioner side of PB-GATT over one Mesh Provisioning Service connection.

    Every wait has its own timeout and every failure raises ``ProvisioningError`` naming the
    step. The caller owns the transport (and closes it). Nothing here touches a network's
    sequence numbers: provisioning PDUs are not network PDUs.
    """

    def __init__(
        self,
        transport: GattTransport,
        *,
        private_key: ec.EllipticCurvePrivateKey | None = None,
        random: Callable[[int], bytes] = os.urandom,
        attention: int = ATTENTION_SECONDS,
    ) -> None:
        self._transport = transport
        self._private_key = private_key
        self._random = random
        self._attention = attention
        self._reassembler = ProxyReassembler()
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def provision(
        self,
        *,
        net_key: bytes,
        net_key_index: int,
        iv_index: int,
        flags: int,
        assign: Callable[[Capabilities], Awaitable[int]],
    ) -> ProvisioningResult:
        """Provision the device and return its address and DevKey.

        ``assign`` gets the device's capabilities (its element count) and returns the unicast
        address to give it. It runs before anything is sent that commits the device, so it can
        reserve the address; whatever it raises propagates unchanged.
        """
        transport = self._transport
        transport.set_notify_callback(self._on_notify)
        transport.set_disconnect_callback(lambda: self._inbox.put_nowait(None))
        private_key = self._private_key or generate_private_key()
        public_p = public_key_bytes(private_key)

        invite = bytes([self._attention])
        await self._send("invite", PROV_INVITE, invite)
        caps_raw = await self._expect("capabilities", PROV_CAPABILITIES, CAPABILITIES_TIMEOUT)
        caps = parse_capabilities(caps_raw)
        if caps.elements < 1:
            raise ProvisioningError("capabilities", "the device reports no elements")
        if not caps.algorithms & ALGORITHM_FIPS_P256:
            raise ProvisioningError("capabilities", "the device does not support P-256")
        if caps.static_oob_type & STATIC_OOB_ONLY:
            raise OobRequired(
                "capabilities", "the device only accepts OOB authentication, which is unsupported"
            )
        unicast = await assign(caps)

        await self._send("start", PROV_START, START_NO_OOB)
        await asyncio.sleep(START_DELAY)
        await self._send("public_key", PROV_PUBLIC_KEY, public_p)
        public_d = await self._expect("public_key", PROV_PUBLIC_KEY, PUBLIC_KEY_TIMEOUT)
        if hmac.compare_digest(public_d, public_p):
            raise ProvisioningError("public_key", "the device echoed our public key")
        try:
            secret = ecdh_secret(private_key, public_d)
        except MalformedPDUError as err:
            raise ProvisioningError("public_key", str(err)) from err

        salt = confirmation_salt(
            confirmation_inputs(invite, caps_raw, START_NO_OOB, public_p, public_d)
        )
        key = confirmation_key(secret, salt)
        random_p = self._random(16)
        confirmation_p = confirmation(key, random_p)
        await self._send("confirmation", PROV_CONFIRMATION, confirmation_p)
        confirmation_d = await self._expect("confirmation", PROV_CONFIRMATION, CONFIRMATION_TIMEOUT)
        if hmac.compare_digest(confirmation_d, confirmation_p):
            raise ProvisioningError("confirmation", "the device reflected our confirmation")
        await self._send("random", PROV_RANDOM, random_p)
        random_d = await self._expect("random", PROV_RANDOM, RANDOM_TIMEOUT)
        if not hmac.compare_digest(confirmation(key, random_d), confirmation_d):
            raise ProvisioningError("random", "the device's confirmation does not match")

        prov_salt = provisioning_salt(salt, random_p, random_d)
        data = ProvisioningData(net_key, net_key_index, flags, iv_index, unicast)
        encrypted = encrypt_provisioning_data(
            session_key(secret, prov_salt), session_nonce(secret, prov_salt), data
        )
        await self._send("data", PROV_DATA, encrypted)
        await self._expect("complete", PROV_COMPLETE, COMPLETE_TIMEOUT)
        return ProvisioningResult(
            unicast=unicast,
            element_count=caps.elements,
            dev_key=device_key(secret, prov_salt),
            capabilities=caps,
        )

    def _on_notify(self, data: bytes) -> None:
        message = self._reassembler.feed(bytes(data))
        if message is not None and message[0] == PDU_PROVISIONING:
            self._inbox.put_nowait(message[1])

    async def _send(self, step: str, pdu_type: int, params: bytes) -> None:
        if not self._transport.is_connected:
            raise ProvisioningError(step, "the link dropped")
        payload = prov_pdu(pdu_type, params)
        try:
            for part in proxy_pdus(PDU_PROVISIONING, payload, self._transport.max_write):
                await self._transport.write(part)
        except Exception as err:  # bleak and the HA bluetooth stack raise their own types
            raise ProvisioningError(step, f"write failed: {err}") from err

    async def _expect(self, step: str, pdu_type: int, timeout: float) -> bytes:
        """The next provisioning PDU, which must be ``pdu_type``; its parameters."""
        try:
            async with asyncio.timeout(timeout):
                pdu = await self._inbox.get()
        except TimeoutError as err:
            raise ProvisioningError(step, f"no answer in {timeout:.0f} s") from err
        if pdu is None:
            raise ProvisioningError(step, "the link dropped")
        try:
            got, params = parse_prov_pdu(pdu)
        except MalformedPDUError as err:
            raise ProvisioningError(step, str(err)) from err
        if got == PROV_FAILED:
            raise ProvisioningRefused(step, params[0])
        if got != pdu_type:
            raise ProvisioningError(step, f"unexpected PDU 0x{got:02X}")
        _LOGGER.debug("Provisioning: %s done", step)
        return params


# finding and provisioning a device


@dataclass(frozen=True)
class UnprovisionedDevice:
    """A factory-reset node advertising the Mesh Provisioning Service (0x1827)."""

    address: str  # Bluetooth MAC, upper case
    name: str | None
    rssi: int | None
    uuid: bytes  # Device UUID
    oob_info: int
    remote: bool = False  # seen only through a remote (ESPHome) Bluetooth proxy


def parse_provisioning_service_data(data: bytes) -> tuple[bytes, int] | None:
    """The Device UUID and OOB Information of 0x1827 service data; None if it is not valid."""
    if len(data) != 18:
        return None
    return bytes(data[:16]), int.from_bytes(data[16:18], "big")


class ProvisioningRadio(Protocol):
    def unprovisioned(self) -> list[UnprovisionedDevice]: ...

    async def connect_provisioning(self, address: str) -> GattTransport: ...


async def provision_device(
    radio: ProvisioningRadio,
    address: str,
    *,
    net_key: bytes,
    net_key_index: int,
    iv_index: int,
    flags: int = 0,
    assign: Callable[[Capabilities], Awaitable[int]],
) -> ProvisioningResult:
    """Connect to ``address`` over PB-GATT, provision it, and always close the link."""
    try:
        transport = await radio.connect_provisioning(address)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        raise ProvisioningError("connect", f"cannot connect: {err}") from err
    try:
        return await Provisioner(transport).provision(
            net_key=net_key,
            net_key_index=net_key_index,
            iv_index=iv_index,
            flags=flags,
            assign=assign,
        )
    finally:
        with contextlib.suppress(Exception):
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await transport.close()
