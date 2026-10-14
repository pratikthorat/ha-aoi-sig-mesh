import pytest

from mesh.pdu import (
    FILTER_ACCEPT,
    MAX_PROXY_MESSAGE,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    FilterStatus,
    NetworkKeys,
    ProxyReassembler,
    SecureBeacon,
    add_addresses,
    decrypt_network_pdu,
    decrypt_proxy_config,
    encrypt_network_pdu,
    encrypt_proxy_config,
    make_secure_beacon,
    parse_filter_status,
    parse_secure_beacon,
    proxy_pdus,
    set_filter_type,
)

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


def test_proxy_pdus_complete_when_it_fits():
    assert proxy_pdus(PDU_NETWORK, b"\x11" * 19, 20) == [b"\x00" + b"\x11" * 19]


def test_proxy_pdus_split_first_continue_last():
    payload = bytes(range(50))
    parts = proxy_pdus(PDU_PROXY_CONFIG, payload, 20)
    assert [p[0] for p in parts] == [0x42, 0x82, 0xC2]
    assert [len(p) for p in parts] == [20, 20, 13]
    assert b"".join(p[1:] for p in parts) == payload


def test_proxy_pdus_rejects_tiny_mtu():
    with pytest.raises(ValueError):
        proxy_pdus(PDU_NETWORK, b"x", 1)


def test_reassembler_round_trip():
    payload = bytes(range(50))
    rx = ProxyReassembler()
    results = [rx.feed(p) for p in proxy_pdus(PDU_NETWORK, payload, 20)]
    assert results[:-1] == [None, None]
    assert results[-1] == (PDU_NETWORK, payload)


def test_reassembler_complete_passes_through():
    assert ProxyReassembler().feed(b"\x01\xaa\xbb") == (1, b"\xaa\xbb")


def test_reassembler_drops_junk():
    rx = ProxyReassembler()
    assert rx.feed(b"") is None
    assert rx.feed(b"\x80\x01\x02") is None  # continuation with no first part
    assert rx.feed(b"\xc0\x01\x02") is None  # last with no first part
    assert rx.feed(b"\x40\x01") is None
    assert rx.feed(b"\x82\x02") is None  # type changes mid-message -> reset
    assert rx.feed(b"\xc0\x03") is None  # the old partial is gone
    assert rx.feed(b"\x00\x09") == (0, b"\x09")


def test_reassembler_complete_discards_partial():
    rx = ProxyReassembler()
    rx.feed(b"\x40\x01")
    assert rx.feed(b"\x00\x09") == (0, b"\x09")
    assert rx.feed(b"\xc0\x02") is None


def test_reassembler_caps_message_size():
    rx = ProxyReassembler()
    assert rx.feed(b"\x40" + bytes(300)) is None
    assert rx.feed(b"\x80" + bytes(300)) is None  # over MAX_PROXY_MESSAGE -> dropped
    assert rx.feed(b"\xc0\x01") is None
    assert MAX_PROXY_MESSAGE == 512


SAMPLE_BEACON = bytes.fromhex("01003ecaff672f673370123456788ea261582f364f6f")


def test_make_secure_beacon_spec_sample():
    assert make_secure_beacon(KEYS, IV) == SAMPLE_BEACON


def test_parse_secure_beacon_spec_sample():
    assert parse_secure_beacon(KEYS, SAMPLE_BEACON) == SecureBeacon(False, False, IV)


def test_beacon_flags_round_trip():
    data = make_secure_beacon(KEYS, IV + 1, iv_update=True, key_refresh=True)
    assert data[1] == 0x03
    assert parse_secure_beacon(KEYS, data) == SecureBeacon(True, True, IV + 1)


def test_parse_secure_beacon_rejects_bad_input():
    other = NetworkKeys.from_net_key(bytes(16))
    assert parse_secure_beacon(other, SAMPLE_BEACON) is None  # other network
    forged = bytearray(SAMPLE_BEACON)
    forged[10] ^= 0x01  # change the IV index without fixing the auth value
    assert parse_secure_beacon(KEYS, bytes(forged)) is None
    assert parse_secure_beacon(KEYS, SAMPLE_BEACON[:-1]) is None
    assert parse_secure_beacon(KEYS, b"\x00" + SAMPLE_BEACON[1:]) is None  # unprovisioned type
    assert parse_secure_beacon(KEYS, b"") is None


def test_proxy_config_round_trip():
    message = set_filter_type(FILTER_ACCEPT)
    pdu = encrypt_proxy_config(KEYS, seq=7, src=0x0001, iv_index=IV, message=message)
    net = decrypt_proxy_config(KEYS, pdu, IV)
    assert (net.ctl, net.ttl, net.seq, net.src, net.dst) == (1, 0, 7, 0x0001, 0x0000)
    assert net.transport_pdu == message


def test_proxy_config_nonce_differs_from_network_nonce():
    pdu = encrypt_proxy_config(KEYS, seq=7, src=0x0001, iv_index=IV, message=b"\x00\x00")
    assert decrypt_network_pdu(KEYS, pdu, IV) is None
    net_pdu = encrypt_network_pdu(
        KEYS, ctl=1, ttl=0, seq=7, src=0x0001, dst=0, transport_pdu=b"\x00\x00", iv_index=IV
    )
    assert decrypt_proxy_config(KEYS, net_pdu, IV) is None


def test_filter_messages():
    assert set_filter_type(FILTER_ACCEPT) == b"\x00\x00"
    assert add_addresses(0x0001, 0x00B0) == bytes.fromhex("01000100b0")


def test_parse_filter_status():
    assert parse_filter_status(bytes.fromhex("03000001")) == FilterStatus(0, 1)
    assert parse_filter_status(bytes.fromhex("030000")) is None
    assert parse_filter_status(bytes.fromhex("04000001")) is None
