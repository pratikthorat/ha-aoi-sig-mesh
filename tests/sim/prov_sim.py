"""The device side of PB-GATT for the simulator: a factory-reset node waiting to be provisioned."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric import ec

from mesh.errors import LinkClosedError, MalformedPDUError, MeshAuthError
from mesh.pdu import PDU_PROVISIONING, ProxyReassembler, proxy_pdus
from mesh.provisioner import (
    ALGORITHM_FIPS_P256,
    FAILED_CONFIRMATION,
    FAILED_DECRYPTION,
    FAILED_INVALID_FORMAT,
    FAILED_OUT_OF_RESOURCES,
    FAILED_UNEXPECTED_PDU,
    PROV_CAPABILITIES,
    PROV_COMPLETE,
    PROV_CONFIRMATION,
    PROV_DATA,
    PROV_FAILED,
    PROV_INVITE,
    PROV_PUBLIC_KEY,
    PROV_RANDOM,
    PROV_START,
    START_NO_OOB,
    Capabilities,
    ProvisioningData,
    confirmation,
    confirmation_inputs,
    confirmation_key,
    confirmation_salt,
    decrypt_provisioning_data,
    device_key,
    ecdh_secret,
    generate_private_key,
    parse_prov_pdu,
    prov_pdu,
    provisioning_salt,
    public_key_bytes,
    session_key,
    session_nonce,
)

STALL_STEPS = ("capabilities", "public_key", "confirmation", "random", "complete")
ELEMENTS = {"ctl": 2, "lightness": 1, "onoff": 1}


class ProvisioningLoopback:
    """A fake GATT link to an unprovisioned device (implements mesh.proxy.GattTransport)."""

    def __init__(self, on_write: Callable[[bytes], None], max_write: int = 20) -> None:
        self._on_write = on_write
        self.max_write = max_write
        self.is_connected = True
        self.writes: list[bytes] = []
        self._notify: Callable[[bytes], None] | None = None
        self._disconnect: Callable[[], None] | None = None

    def set_notify_callback(self, callback: Callable[[bytes], None]) -> None:
        self._notify = callback

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._disconnect = callback

    async def write(self, data: bytes) -> None:
        if not self.is_connected:
            raise LinkClosedError("provisioning link is closed")
        self.writes.append(bytes(data))
        self._on_write(bytes(data))

    async def close(self) -> None:
        self.drop()

    def drop(self) -> None:
        if not self.is_connected:
            return
        self.is_connected = False
        if self._disconnect is not None:
            asyncio.get_running_loop().call_soon(self._disconnect)

    def notify(self, data: bytes) -> None:
        if self.is_connected:
            asyncio.get_running_loop().call_soon(self._deliver, bytes(data))

    def _deliver(self, data: bytes) -> None:
        if self.is_connected and self._notify is not None:
            self._notify(data)


@dataclass
class SimUnprovisioned:
    """A factory-reset node advertising the Mesh Provisioning Service (0x1827).

    Failure switches: ``refuse`` answers the Invite with Provisioning Failed,
    ``bad_confirmation`` sends a confirmation that will not verify, ``stall_at`` never answers
    that step (at "complete" the device has taken the data: it is provisioned but never says
    so), ``oob_only`` claims OOB-only authentication.
    """

    address: str
    uuid: bytes = field(default_factory=lambda: os.urandom(16))
    kind: str = "onoff"
    oob_info: int = 0
    name: str | None = None
    refuse: bool = False
    bad_confirmation: bool = False
    stall_at: str | None = None
    oob_only: bool = False
    private_key: ec.EllipticCurvePrivateKey | None = None
    random: bytes | None = None
    on_provisioned: Callable[[SimUnprovisioned], None] | None = None
    provisioned: ProvisioningData | None = None
    dev_key: bytes | None = field(default=None, repr=False)
    received: list[int] = field(default_factory=list)
    _transport: ProvisioningLoopback | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.stall_at is not None and self.stall_at not in STALL_STEPS:
            raise ValueError(f"unknown step {self.stall_at!r}")
        self._reset_session()

    @property
    def element_count(self) -> int:
        return ELEMENTS[self.kind]

    @property
    def service_data(self) -> bytes:
        """0x1827 service data: the Device UUID, then the OOB information (big-endian)."""
        return self.uuid + self.oob_info.to_bytes(2, "big")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            self.element_count, ALGORITHM_FIPS_P256, 0, 0x02 if self.oob_only else 0, 0, 0, 0, 0
        )

    def connect(self, max_write: int = 20) -> ProvisioningLoopback:
        """A new PB-GATT connection; a new connection starts a new provisioning session."""
        self._reset_session()
        self._transport = ProvisioningLoopback(self._on_write, max_write)
        return self._transport

    def _reset_session(self) -> None:
        self._rx = ProxyReassembler()
        self._invite: bytes | None = None
        self._caps = self.capabilities().encode()
        self._start: bytes | None = None
        self._public_p: bytes | None = None
        self._secret: bytes | None = None
        self._key = self._salt = b""
        self._confirmation_p: bytes | None = None
        self._random_d = b""
        self._done = False

    # traffic from the provisioner

    def _send(self, pdu_type: int, params: bytes = b"") -> None:
        transport = self._transport
        if transport is None:
            return
        for part in proxy_pdus(PDU_PROVISIONING, prov_pdu(pdu_type, params), transport.max_write):
            transport.notify(part)

    def _fail(self, code: int) -> None:
        self._done = True
        self._send(PROV_FAILED, bytes([code]))

    def _on_write(self, data: bytes) -> None:
        message = self._rx.feed(data)
        if message is None or message[0] != PDU_PROVISIONING or self._done:
            return
        try:
            pdu_type, params = parse_prov_pdu(message[1])
        except MalformedPDUError:
            self._fail(FAILED_INVALID_FORMAT)
            return
        self.received.append(pdu_type)
        handler = {
            PROV_INVITE: self._on_invite,
            PROV_START: self._on_start,
            PROV_PUBLIC_KEY: self._on_public_key,
            PROV_CONFIRMATION: self._on_confirmation,
            PROV_RANDOM: self._on_random,
            PROV_DATA: self._on_data,
        }.get(pdu_type)
        if handler is None:
            self._fail(FAILED_UNEXPECTED_PDU)
            return
        handler(params)

    def _on_invite(self, params: bytes) -> None:
        if self.refuse:
            self._fail(FAILED_OUT_OF_RESOURCES)
            return
        self._invite = params
        if self.stall_at != "capabilities":
            self._send(PROV_CAPABILITIES, self._caps)

    def _on_start(self, params: bytes) -> None:
        if self._invite is None:
            self._fail(FAILED_UNEXPECTED_PDU)
        elif params != START_NO_OOB:
            self._fail(FAILED_INVALID_FORMAT)
        else:
            self._start = params

    def _on_public_key(self, params: bytes) -> None:
        if self._start is None or self._invite is None:
            self._fail(FAILED_UNEXPECTED_PDU)
            return
        private_key = self.private_key or generate_private_key()
        public_d = public_key_bytes(private_key)
        try:
            self._secret = ecdh_secret(private_key, params)
        except MalformedPDUError:
            self._fail(FAILED_INVALID_FORMAT)
            return
        self._public_p = params
        self._salt = confirmation_salt(
            confirmation_inputs(self._invite, self._caps, self._start, params, public_d)
        )
        self._key = confirmation_key(self._secret, self._salt)
        if self.stall_at != "public_key":
            self._send(PROV_PUBLIC_KEY, public_d)

    def _on_confirmation(self, params: bytes) -> None:
        if self._secret is None:
            self._fail(FAILED_UNEXPECTED_PDU)
            return
        self._confirmation_p = params
        self._random_d = self.random or os.urandom(16)
        value = confirmation(self._key, self._random_d)
        if self.bad_confirmation:
            value = bytes(b ^ 0xFF for b in value)
        if self.stall_at != "confirmation":
            self._send(PROV_CONFIRMATION, value)

    def _on_random(self, params: bytes) -> None:
        if self._confirmation_p is None or self._secret is None:
            self._fail(FAILED_UNEXPECTED_PDU)
            return
        if confirmation(self._key, params) != self._confirmation_p:
            self._fail(FAILED_CONFIRMATION)
            return
        self._salt = provisioning_salt(self._salt, params, self._random_d)
        if self.stall_at != "random":
            self._send(PROV_RANDOM, self._random_d)

    def _on_data(self, params: bytes) -> None:
        if self._secret is None or not self._random_d:
            self._fail(FAILED_UNEXPECTED_PDU)
            return
        secret, salt = self._secret, self._salt
        try:
            data = decrypt_provisioning_data(
                session_key(secret, salt), session_nonce(secret, salt), params
            )
        except (MeshAuthError, MalformedPDUError):
            self._fail(FAILED_DECRYPTION)
            return
        self._done = True
        self.provisioned = data
        self.dev_key = device_key(secret, salt)
        if self.on_provisioned is not None:
            self.on_provisioned(self)
        if self.stall_at != "complete":
            self._send(PROV_COMPLETE)
