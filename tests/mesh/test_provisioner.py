"""Provisioning PDUs and crypto against the Mesh Profile section 8.7 sample data."""

import asyncio
import pickle

import pytest

from mesh import provisioner
from mesh.errors import (
    MalformedPDUError,
    MeshAuthError,
    MeshCryptoError,
    MeshError,
    ProvisioningError,
)
from mesh.pdu import PDU_PROVISIONING, ProxyReassembler, proxy_pdus
from mesh.provisioner import (
    FAILED_CONFIRMATION,
    FAILED_OUT_OF_RESOURCES,
    P256_ORDER,
    P256_PRIME,
    PROV_CAPABILITIES,
    PROV_COMPLETE,
    PROV_CONFIRMATION,
    PROV_DATA,
    PROV_FAILED,
    PROV_INPUT_COMPLETE,
    PROV_INVITE,
    PROV_PUBLIC_KEY,
    PROV_RANDOM,
    PROV_START,
    START_NO_OOB,
    Capabilities,
    OobRequired,
    Provisioner,
    ProvisioningData,
    ProvisioningRefused,
    confirmation,
    confirmation_inputs,
    confirmation_key,
    confirmation_salt,
    decrypt_provisioning_data,
    device_key,
    ecdh_secret,
    encrypt_provisioning_data,
    generate_private_key,
    parse_capabilities,
    parse_prov_pdu,
    parse_provisioning_data,
    private_key_from_bytes,
    prov_pdu,
    provision_device,
    provisioning_salt,
    public_key_bytes,
    session_key,
    session_nonce,
)
from tests.sim.mesh_sim import NET_KEY as SIM_NET_KEY
from tests.sim.mesh_sim import run
from tests.sim.prov_sim import STALL_STEPS, SimUnprovisioned

h = bytes.fromhex

# Mesh Profile 1.0.1, section 8.7 (provisioning sample data)
PRIVATE_P = h("06a516693c9aa31a6084545d0c5db641b48572b97203ddffb7ac73f7d0457663")
PUBLIC_P = h(
    "2c31a47b5779809ef44cb5eaaf5c3e43d5f8faad4a8794cb987e9b03745c78dd"
    "919512183898dfbecd52e2408e43871fd021109117bd3ed4eaf8437743715d4f"
)
PRIVATE_D = h("529aa0670d72cd6497502ed473502b037e8803b5c60829a5a3caa219505530ba")
PUBLIC_D = h(
    "f465e43ff23d3f1b9dc7dfc04da8758184dbc966204796eccf0d6cf5e16500cc"
    "0201d048bcbbd899eeefc424164e33c201c2b010ca6b4d43a8a155cad8ecb279"
)
ECDH = h("ab85843a2f6d883f62e5684b38e307335fe6e1945ecd19604105c6f23221eb69")
INVITE = h("00")
CAPS = h("0100010000000000000000")
CONF_SALT = h("5faabe187337c71cc6c973369dcaa79a")
CONF_KEY = h("e31fe046c68ec339c425fc6629f0336f")
RANDOM_P = h("8b19ac31d58b124c946209b5db1021b9")
RANDOM_D = h("55a2a2bca04cd32ff6f346bd0a0c1a3a")
CONF_P = h("b38a114dfdca1fe153bd2c1e0dc46ac2")
CONF_D = h("eeba521c196b52cc2e37aa40329f554e")
PROV_SALT = h("a21c7d45f201cf9489a2fb57145015b4")
SESSION_KEY = h("c80253af86b33dfa450bbdb2a191fea3")
SESSION_NONCE = h("da7ddbe78b5f62b81d6847487e")
NET_KEY = h("efb2255e6422d330088e09bb015ed707")
DATA = ProvisioningData(NET_KEY, 0x0567, 0x00, 0x01020304, 0x0B0C)
ENCRYPTED = h("d0bd7f4a89a2ff6222af59a90a60ad58acfe3123356f5cec2973e0ec50783b10c7")
DEV_KEY = h("0520adad5e0142aa3e325087b4ec16d8")


def test_public_keys_and_ecdh_match_the_sample_data():
    key_p, key_d = private_key_from_bytes(PRIVATE_P), private_key_from_bytes(PRIVATE_D)
    assert public_key_bytes(key_p) == PUBLIC_P
    assert public_key_bytes(key_d) == PUBLIC_D
    assert ecdh_secret(key_p, PUBLIC_D) == ECDH
    assert ecdh_secret(key_d, PUBLIC_P) == ECDH


def test_confirmation_values_match_the_sample_data():
    inputs = confirmation_inputs(INVITE, CAPS, START_NO_OOB, PUBLIC_P, PUBLIC_D)
    assert len(inputs) == 145
    salt = confirmation_salt(inputs)
    assert salt == CONF_SALT
    key = confirmation_key(ECDH, salt)
    assert key == CONF_KEY
    assert confirmation(key, RANDOM_P) == CONF_P
    assert confirmation(key, RANDOM_D) == CONF_D


def test_session_keys_data_and_device_key_match_the_sample_data():
    salt = provisioning_salt(CONF_SALT, RANDOM_P, RANDOM_D)
    assert salt == PROV_SALT
    assert session_key(ECDH, salt) == SESSION_KEY
    assert session_nonce(ECDH, salt) == SESSION_NONCE
    assert encrypt_provisioning_data(SESSION_KEY, SESSION_NONCE, DATA) == ENCRYPTED
    assert decrypt_provisioning_data(SESSION_KEY, SESSION_NONCE, ENCRYPTED) == DATA
    assert device_key(ECDH, salt) == DEV_KEY


def test_a_tampered_provisioning_data_fails_its_mic():
    tampered = bytes([ENCRYPTED[0] ^ 1]) + ENCRYPTED[1:]
    with pytest.raises(MeshAuthError):
        decrypt_provisioning_data(SESSION_KEY, SESSION_NONCE, tampered)


def test_capabilities_round_trip():
    caps = parse_capabilities(CAPS)
    assert caps == Capabilities(1, 0x0001, 0, 0, 0, 0, 0, 0)
    assert caps.encode() == CAPS
    with pytest.raises(MalformedPDUError):
        parse_capabilities(CAPS[:-1])


def test_provisioning_data_never_shows_the_net_key():
    assert NET_KEY.hex() not in repr(DATA)


@pytest.mark.parametrize(
    "point",
    [bytes(64), PUBLIC_D[:32] + bytes([PUBLIC_D[32] ^ 1]) + PUBLIC_D[33:], PUBLIC_D[:63]],
    ids=["zero", "off-curve", "short"],
)
def test_an_invalid_public_key_is_malformed(point):
    with pytest.raises(MalformedPDUError):
        ecdh_secret(private_key_from_bytes(PRIVATE_P), point)


def test_pdu_parsing_checks_type_and_length():
    assert parse_prov_pdu(prov_pdu(PROV_COMPLETE)) == (PROV_COMPLETE, b"")
    assert parse_prov_pdu(prov_pdu(PROV_CAPABILITIES, CAPS)) == (PROV_CAPABILITIES, CAPS)
    for bad in (b"", b"\x0a", b"\x41" + CAPS, prov_pdu(PROV_PUBLIC_KEY, PUBLIC_P[:10])):
        with pytest.raises(MalformedPDUError):
            parse_prov_pdu(bad)


# the provisioner against the simulated device


def fast_timeouts(monkeypatch):
    for name in (
        "CAPABILITIES_TIMEOUT",
        "PUBLIC_KEY_TIMEOUT",
        "CONFIRMATION_TIMEOUT",
        "RANDOM_TIMEOUT",
        "COMPLETE_TIMEOUT",
    ):
        monkeypatch.setattr(provisioner, name, 0.1)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    fast_timeouts(monkeypatch)


async def provision(device, *, unicast=0x0010, assigned=None, **kwargs):
    transport = device.connect()

    async def assign(caps):
        if assigned is not None:
            assigned.append(caps)
        return unicast

    provisioner_ = Provisioner(transport, **kwargs)
    return await provisioner_.provision(
        net_key=SIM_NET_KEY, net_key_index=0, iv_index=0, flags=0, assign=assign
    )


def written_pdus(transport):
    """The provisioning PDUs the provisioner wrote, reassembled."""
    rx, pdus = ProxyReassembler(), []
    for part in transport.writes:
        if (message := rx.feed(part)) is not None:
            pdus.append(message[1])
    return pdus


def test_provisioning_reproduces_the_sample_data_on_the_air():
    async def scenario():
        device = SimUnprovisioned(
            "AA:00:00:00:00:27", private_key=private_key_from_bytes(PRIVATE_D), random=RANDOM_D
        )
        transport = device.connect()

        async def assign(caps):
            assert caps.encode() == CAPS
            return 0x0B0C

        result = await Provisioner(
            transport,
            private_key=private_key_from_bytes(PRIVATE_P),
            random=lambda n: RANDOM_P,
            attention=0,
        ).provision(
            net_key=NET_KEY, net_key_index=0x0567, iv_index=0x01020304, flags=0, assign=assign
        )
        assert result.dev_key == DEV_KEY
        assert result.unicast == 0x0B0C
        assert result.element_count == 1
        assert device.provisioned == DATA
        assert device.dev_key == DEV_KEY
        assert written_pdus(transport) == [
            prov_pdu(PROV_INVITE, INVITE),
            prov_pdu(PROV_START, START_NO_OOB),
            prov_pdu(PROV_PUBLIC_KEY, PUBLIC_P),
            prov_pdu(PROV_CONFIRMATION, CONF_P),
            prov_pdu(PROV_RANDOM, RANDOM_P),
            prov_pdu(PROV_DATA, ENCRYPTED),
        ]

    run(scenario())


def test_random_keys_provision_a_two_element_light():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", kind="ctl")
        assigned = []
        result = await provision(device, unicast=0x0042, assigned=assigned)
        assert [caps.elements for caps in assigned] == [2]
        assert result.element_count == 2
        assert result.dev_key == device.dev_key
        assert device.provisioned.unicast == 0x0042
        assert device.provisioned.net_key == SIM_NET_KEY
        assert result.dev_key.hex() not in repr(result)

    run(scenario())


def test_a_refusing_device_raises_refused_with_its_code():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", refuse=True)
        with pytest.raises(ProvisioningRefused) as caught:
            await provision(device)
        assert caught.value.step == "capabilities"
        assert caught.value.code == FAILED_OUT_OF_RESOURCES
        assert "out of resources" in str(caught.value)

    run(scenario())


def test_a_bad_device_confirmation_fails_at_random_and_sends_no_data():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", bad_confirmation=True)
        with pytest.raises(ProvisioningError) as caught:
            await provision(device)
        assert caught.value.step == "random"
        assert PROV_DATA not in device.received
        assert device.provisioned is None

    run(scenario())


@pytest.mark.parametrize("step", STALL_STEPS)
def test_a_stall_at_each_step_times_out_naming_it(step):
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", stall_at=step)
        with pytest.raises(ProvisioningError) as caught:
            await provision(device)
        assert caught.value.step == step
        assert "no answer" in caught.value.message
        # only a stall at "complete" leaves the device holding the network key
        assert (device.provisioned is not None) == (step == "complete")

    run(scenario())


def test_an_oob_only_device_is_refused_before_an_address_is_assigned():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", oob_only=True)
        assigned = []
        with pytest.raises(OobRequired) as caught:
            await provision(device, assigned=assigned)
        assert caught.value.step == "capabilities"
        assert assigned == []
        assert device.received == [PROV_INVITE]

    run(scenario())


def test_an_assign_error_propagates_and_nothing_more_is_sent():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27")
        transport = device.connect()

        async def assign(caps):
            raise MeshError("no free address")

        with pytest.raises(MeshError, match="no free address"):
            await Provisioner(transport).provision(
                net_key=SIM_NET_KEY, net_key_index=0, iv_index=0, flags=0, assign=assign
            )
        assert device.received == [PROV_INVITE]

    run(scenario())


def test_a_dropped_link_names_the_step():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27")
        transport = device.connect()

        async def assign(caps):
            transport.drop()
            return 0x0010

        with pytest.raises(ProvisioningError) as caught:
            await Provisioner(transport).provision(
                net_key=SIM_NET_KEY, net_key_index=0, iv_index=0, flags=0, assign=assign
            )
        assert caught.value.step == "start"
        assert caught.value.message == "the link dropped"

    run(scenario())


def test_a_drop_while_waiting_is_reported_at_once():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", stall_at="public_key")
        transport = device.connect()
        provisioner.PUBLIC_KEY_TIMEOUT = 5.0  # restored by the fixture's monkeypatch

        async def drop_soon():
            await asyncio.sleep(0.05)
            transport.drop()

        dropper = asyncio.ensure_future(drop_soon())
        with pytest.raises(ProvisioningError) as caught:
            await Provisioner(transport).provision(
                net_key=SIM_NET_KEY, net_key_index=0, iv_index=0, flags=0, assign=assign_0x10
            )
        await dropper
        assert caught.value.step == "public_key"
        assert caught.value.message == "the link dropped"

    run(scenario())


async def assign_0x10(caps):
    return 0x0010


def test_a_device_echoing_our_public_key_is_refused():
    async def scenario():
        same = private_key_from_bytes(PRIVATE_P)
        device = SimUnprovisioned("AA:00:00:00:00:27", private_key=same)
        with pytest.raises(ProvisioningError) as caught:
            await provision(device, private_key=same)
        assert caught.value.step == "public_key"
        assert "echoed" in caught.value.message

    run(scenario())


class Reflector(SimUnprovisioned):
    """Sends our own confirmation back: the reflection attack."""

    def _on_confirmation(self, params):
        self._send(PROV_CONFIRMATION, params)


class BadPoint(SimUnprovisioned):
    """Answers with a public key that is not on the curve."""

    def _on_public_key(self, params):
        self._send(PROV_PUBLIC_KEY, bytes(64))


@pytest.mark.parametrize(
    ("device_class", "step"), [(Reflector, "confirmation"), (BadPoint, "public_key")]
)
def test_hostile_devices_fail_before_any_key_is_sent(device_class, step):
    async def scenario():
        device = device_class("AA:00:00:00:00:27")
        with pytest.raises(ProvisioningError) as caught:
            await provision(device)
        assert caught.value.step == step
        assert PROV_DATA not in device.received

    run(scenario())


def test_refused_errors_pickle():
    error = pickle.loads(pickle.dumps(ProvisioningRefused("random", FAILED_CONFIRMATION)))
    assert (error.step, error.code) == ("random", FAILED_CONFIRMATION)
    assert "confirmation failed" in str(error)


# polish: input checks and pinned edges


P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B


def small_x_point() -> tuple[int, int]:
    """A real P-256 point with the smallest x (so x + p still fits in 32 bytes)."""
    for x in range(1, 100):
        rhs = (x**3 - 3 * x + P256_B) % P256_PRIME
        y = pow(rhs, (P256_PRIME + 1) // 4, P256_PRIME)  # p = 3 mod 4
        if y * y % P256_PRIME == rhs:
            return x, y
    raise AssertionError("no small point found")


def test_a_non_canonical_x_plus_p_public_key_is_malformed():
    """cryptography accepts (x + p, y) for a real point, so this check is the only guard."""
    key = private_key_from_bytes(PRIVATE_P)
    x, y = small_x_point()
    assert x + P256_PRIME < 2**256
    canonical = x.to_bytes(32, "big") + y.to_bytes(32, "big")
    ecdh_secret(key, canonical)  # the point itself is valid
    for bad in (
        (x + P256_PRIME).to_bytes(32, "big") + y.to_bytes(32, "big"),
        x.to_bytes(32, "big") + (y + P256_PRIME).to_bytes(32, "big")
        if y + P256_PRIME < 2**256
        else None,
    ):
        if bad is None:
            continue
        with pytest.raises(MalformedPDUError, match="not a valid P-256 point") as caught:
            ecdh_secret(key, bad)
        assert caught.value.__cause__ is None  # our check fired, not cryptography's


def test_a_public_key_coordinate_at_or_above_p_is_malformed():
    key = private_key_from_bytes(PRIVATE_P)
    x, y = PUBLIC_D[:32], PUBLIC_D[32:]
    for bad in (
        P256_PRIME.to_bytes(32, "big") + y,
        x + P256_PRIME.to_bytes(32, "big"),
        bytes([0xFF]) * 64,
    ):
        with pytest.raises(MalformedPDUError, match="not a valid P-256 point"):
            ecdh_secret(key, bad)


def test_decrypting_provisioning_data_checks_its_length_first():
    for bad in (b"", ENCRYPTED[:-1], ENCRYPTED + b"\x00"):
        with pytest.raises(MalformedPDUError, match="must be 33 bytes"):
            decrypt_provisioning_data(SESSION_KEY, SESSION_NONCE, bad)


def test_provisioning_input_complete_has_its_name():
    assert PROV_INPUT_COMPLETE == 0x04
    assert parse_prov_pdu(prov_pdu(PROV_INPUT_COMPLETE)) == (PROV_INPUT_COMPLETE, b"")


@pytest.mark.parametrize(
    "raw",
    [bytes(32), P256_ORDER.to_bytes(32, "big"), bytes([0xFF]) * 32],
    ids=["zero", "n", "max"],
)
def test_a_private_key_outside_1_to_n_minus_1_is_rejected(raw):
    with pytest.raises(MeshCryptoError):
        private_key_from_bytes(raw)
    assert private_key_from_bytes((P256_ORDER - 1).to_bytes(32, "big")) is not None


def test_provisioning_data_must_be_exactly_its_size():
    good = DATA.encode()
    assert parse_provisioning_data(good) == DATA
    for bad in (good[:-1], good + b"\x00", b""):
        with pytest.raises(MalformedPDUError):
            parse_provisioning_data(bad)


def test_a_generated_private_key_is_in_range_and_differs_each_time():
    first, second = generate_private_key(), generate_private_key()
    for key in (first, second):
        assert 1 <= key.private_numbers().private_value < P256_ORDER
    assert first.private_numbers().private_value != second.private_numbers().private_value


def test_a_full_inbox_drops_the_newest_pdu_but_never_the_close_sentinel(caplog):
    async def scenario():
        transport = SimUnprovisioned("AA:00:00:00:00:27").connect()
        prov = Provisioner(transport)
        for i in range(provisioner.INBOX_SIZE + 5):
            payload = prov_pdu(PROV_RANDOM, bytes([i]) * 16)
            for part in proxy_pdus(PDU_PROVISIONING, payload, 20):
                prov._on_notify(part)
        assert prov._inbox.qsize() == provisioner.INBOX_SIZE
        assert prov._inbox.get_nowait()[1] == 0  # the oldest is kept
        prov._inbox.put_nowait(b"x")
        prov._on_disconnect()  # full queue: makes room, never raises or blocks
        items = []
        while not prov._inbox.empty():
            items.append(prov._inbox.get_nowait())
        assert items[-1] is None
        assert items[-2] == b"x"  # queued before the disconnect, still there
        assert items[0][1] == 2  # PDU 1 was the oldest and made room
        assert len(items) == provisioner.INBOX_SIZE

    with caplog.at_level("DEBUG"):
        run(scenario())
    assert "inbox full" in caplog.text


def test_step_timeouts_are_read_when_the_call_is_made(monkeypatch):
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", stall_at="capabilities")
        monkeypatch.setattr(provisioner, "CAPABILITIES_TIMEOUT", 0.05)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ProvisioningError) as caught:
            await provision(device)
        assert asyncio.get_running_loop().time() - started < 1
        assert caught.value.step == "capabilities"

    run(scenario())


def test_timeout_messages_drop_the_trailing_zero(monkeypatch):
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27", stall_at="capabilities")
        monkeypatch.setattr(provisioner, "CAPABILITIES_TIMEOUT", 0.05)
        with pytest.raises(ProvisioningError) as caught:
            await provision(device)
        assert caught.value.message == "no answer in 0.05 s"

    run(scenario())


class FailsAtRandom(SimUnprovisioned):
    def _on_random(self, params):
        self._send(PROV_FAILED, bytes([FAILED_CONFIRMATION]))


def test_a_failed_pdu_at_the_random_step_names_it():
    async def scenario():
        with pytest.raises(ProvisioningRefused) as caught:
            await provision(FailsAtRandom("AA:00:00:00:00:27"))
        assert (caught.value.step, caught.value.code) == ("random", FAILED_CONFIRMATION)

    run(scenario())


def test_a_failed_write_at_the_random_step_names_it():
    async def scenario():
        device = SimUnprovisioned("AA:00:00:00:00:27")
        transport = device.connect()
        real_write = transport.write

        async def write(data):
            if PROV_CONFIRMATION in device.received:  # the Random PDU is the next write
                raise OSError("radio gone")
            await real_write(data)

        transport.write = write
        with pytest.raises(ProvisioningError) as caught:
            await Provisioner(transport).provision(
                net_key=SIM_NET_KEY, net_key_index=0, iv_index=0, flags=0, assign=assign_0x10
            )
        assert caught.value.step == "random"
        assert "write failed" in caught.value.message
        assert PROV_DATA not in device.received

    run(scenario())


def test_a_hanging_provisioning_connect_times_out_at_connect(monkeypatch):
    """A proxy that accepts the connect but never finishes it (or the notify enable)."""

    async def scenario():
        monkeypatch.setattr(provisioner, "PROVISIONING_CONNECT_TIMEOUT", 0.05)
        cancelled = []

        class Radio:
            async def connect_provisioning(self, address):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(address)
                    raise

        started = asyncio.get_running_loop().time()
        with pytest.raises(ProvisioningError) as caught:
            await provision_device(
                Radio(),
                "AA:00:00:00:00:27",
                net_key=SIM_NET_KEY,
                net_key_index=0,
                iv_index=0,
                assign=assign_0x10,
            )
        assert asyncio.get_running_loop().time() - started < 1
        assert (caught.value.step, caught.value.message) == ("connect", "timed out after 0.05 s")
        assert cancelled == ["AA:00:00:00:00:27"]  # the connect itself was stopped

    run(scenario())


def test_the_provisioning_connect_timeout_is_65_s():
    assert provisioner.PROVISIONING_CONNECT_TIMEOUT == 65.0


def test_a_connect_error_of_its_own_is_not_called_a_timeout(monkeypatch):
    async def scenario():
        class Radio:
            async def connect_provisioning(self, address):
                raise TimeoutError("bleak gave up")

        with pytest.raises(ProvisioningError) as caught:
            await provision_device(
                Radio(),
                "AA:00:00:00:00:27",
                net_key=SIM_NET_KEY,
                net_key_index=0,
                iv_index=0,
                assign=assign_0x10,
            )
        assert (caught.value.step, caught.value.message) == (
            "connect",
            "cannot connect: bleak gave up",
        )

    run(scenario())


def test_cancelling_provision_device_closes_the_transport(monkeypatch):
    async def scenario():
        monkeypatch.setattr(provisioner, "CAPABILITIES_TIMEOUT", 30.0)
        device = SimUnprovisioned("AA:00:00:00:00:27", stall_at="capabilities")
        transport = device.connect()

        class Radio:
            async def connect_provisioning(self, address):
                return transport

        task = asyncio.ensure_future(
            provision_device(
                Radio(),
                "AA:00:00:00:00:27",
                net_key=SIM_NET_KEY,
                net_key_index=0,
                iv_index=0,
                assign=assign_0x10,
            )
        )
        await asyncio.sleep(0.05)
        assert transport.is_connected
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not transport.is_connected

    run(scenario())
