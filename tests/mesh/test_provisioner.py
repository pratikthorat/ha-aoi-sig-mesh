"""Provisioning PDUs and crypto against the Mesh Profile section 8.7 sample data."""

import pytest

from mesh.errors import MalformedPDUError, MeshAuthError
from mesh.provisioner import (
    PROV_CAPABILITIES,
    PROV_COMPLETE,
    PROV_PUBLIC_KEY,
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
    encrypt_provisioning_data,
    parse_capabilities,
    parse_prov_pdu,
    private_key_from_bytes,
    prov_pdu,
    provisioning_salt,
    public_key_bytes,
    session_key,
    session_nonce,
)

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
