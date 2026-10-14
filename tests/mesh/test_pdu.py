import pytest

from mesh.pdu import NetworkKeys, decrypt_network_pdu, encrypt_network_pdu

SAMPLE_NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
KEYS = NetworkKeys.from_net_key(SAMPLE_NET_KEY)
IV = 0x12345678
MSG1_TRANSPORT = bytes.fromhex("034b50057e400000010000")
MSG1_PDU = bytes.fromhex("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")


def test_network_keys_from_sample():
    assert KEYS.nid == 0x68
    assert KEYS.enc_key == bytes.fromhex("0953fa93e7caac9638f58820220a398e")
    assert KEYS.priv_key == bytes.fromhex("8b84eedec100067d670971dd2aa700cf")
    assert KEYS.network_id == bytes.fromhex("3ecaff672f673370")
    assert KEYS.beacon_key == bytes.fromhex("5423d967da639a99cb02231a83f7d254")


def test_network_keys_repr_hides_keys():
    text = repr(KEYS)
    assert "0x68" in text
    assert SAMPLE_NET_KEY.hex() not in text
    assert "enc_key" not in text


def test_encrypt_spec_message_1():
    pdu = encrypt_network_pdu(
        KEYS,
        ctl=1,
        ttl=0,
        seq=1,
        src=0x1201,
        dst=0xFFFD,
        transport_pdu=MSG1_TRANSPORT,
        iv_index=IV,
    )
    assert pdu == MSG1_PDU


def test_decrypt_spec_message_1():
    net = decrypt_network_pdu(KEYS, MSG1_PDU, IV)
    assert net is not None
    assert (net.ivi, net.nid, net.ctl, net.ttl) == (0, 0x68, 1, 0)
    assert (net.seq, net.src, net.dst) == (1, 0x1201, 0xFFFD)
    assert net.transport_pdu == MSG1_TRANSPORT


@pytest.mark.parametrize("ctl", [0, 1])
def test_round_trip_access_and_control(ctl):
    pdu = encrypt_network_pdu(
        KEYS,
        ctl=ctl,
        ttl=5,
        seq=0x123456,
        src=0x0001,
        dst=0x00B0,
        transport_pdu=b"\x66" * 12,
        iv_index=IV + 1,
    )
    assert pdu[0] >> 7 == 1  # IVI of an odd IV index
    net = decrypt_network_pdu(KEYS, pdu, IV + 1)
    assert (net.ctl, net.ttl, net.seq, net.src, net.dst) == (ctl, 5, 0x123456, 0x0001, 0x00B0)
    assert net.transport_pdu == b"\x66" * 12


def test_decrypt_rejects_tamper_wrong_iv_short_and_nid():
    tampered = bytearray(MSG1_PDU)
    tampered[-1] ^= 0x01
    assert decrypt_network_pdu(KEYS, bytes(tampered), IV) is None
    assert decrypt_network_pdu(KEYS, MSG1_PDU, IV + 2) is None  # same IVI, wrong IV -> MIC fails
    assert decrypt_network_pdu(KEYS, MSG1_PDU, IV + 1) is None  # IVI parity mismatch
    assert decrypt_network_pdu(KEYS, MSG1_PDU[:13], IV) is None
    assert decrypt_network_pdu(KEYS, b"", IV) is None
    other = NetworkKeys.from_net_key(bytes(16))
    assert decrypt_network_pdu(other, MSG1_PDU, IV) is None
