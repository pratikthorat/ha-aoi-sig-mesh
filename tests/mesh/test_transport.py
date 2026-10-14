import pytest

from mesh.errors import MalformedPDUError
from mesh.transport import (
    SEGMENT_SIZE,
    LowerAccess,
    decrypt_upper,
    encrypt_upper,
    parse_lower_access,
    segmented_access,
    seq_auth_from_segment,
    unsegmented_access,
)

APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
DEV_KEY = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")
IV = 0x12345678
ADDR = {"src": 0x0001, "dst": 0x00B0, "iv_index": IV}


@pytest.mark.parametrize(("key", "akf"), [(APP_KEY, 1), (DEV_KEY, 0)])
@pytest.mark.parametrize("szmic", [0, 1])
def test_upper_round_trip(key, akf, szmic):
    upper = encrypt_upper(
        key, akf=akf, szmic=szmic, seq=42, access_payload=b"\x82\x02\x01\x07", **ADDR
    )
    assert len(upper) == 4 + (8 if szmic else 4)
    assert decrypt_upper(key, akf=akf, szmic=szmic, seq=42, upper=upper, **ADDR) == (
        b"\x82\x02\x01\x07"
    )


def test_upper_nonce_binds_seq_addresses_and_key_type():
    upper = encrypt_upper(APP_KEY, akf=1, szmic=0, seq=42, access_payload=b"\x82\x01", **ADDR)
    assert decrypt_upper(APP_KEY, akf=1, szmic=0, seq=43, upper=upper, **ADDR) is None
    assert decrypt_upper(APP_KEY, akf=0, szmic=0, seq=42, upper=upper, **ADDR) is None
    moved = dict(ADDR, dst=0x00B1)
    assert decrypt_upper(APP_KEY, akf=1, szmic=0, seq=42, upper=upper, **moved) is None


def test_upper_too_short_is_none():
    assert decrypt_upper(APP_KEY, akf=1, szmic=0, seq=1, upper=b"\x00" * 4, **ADDR) is None


def test_unsegmented_header():
    pdu = unsegmented_access(b"\xaa" * 8, akf=1, aid=0x26)
    assert pdu == b"\x66" + b"\xaa" * 8
    assert parse_lower_access(pdu) == LowerAccess(False, 1, 0x26, 0, 0, 0, 0, b"\xaa" * 8)


def test_unsegmented_too_long_raises():
    with pytest.raises(ValueError):
        unsegmented_access(bytes(16), akf=1, aid=0)


def test_segmented_headers():
    segments = segmented_access(bytes(range(24)), akf=0, aid=0, szmic=0, seq_zero=0x0ABC)
    assert segments[0][:4] == bytes.fromhex("802af001")
    assert segments[1][:4] == bytes.fromhex("802af021")
    assert [len(s) for s in segments] == [16, 16]
    szmic = segmented_access(bytes(13), akf=1, aid=0x26, szmic=1, seq_zero=0x0ABC)
    assert szmic[0][:4] == bytes.fromhex("e6aaf001")
    assert len(szmic) == 2 and len(szmic[1]) == 5


def test_segmented_parse_round_trip():
    segments = segmented_access(bytes(range(30)), akf=1, aid=0x26, szmic=0, seq_zero=0x1FFF)
    parsed = [parse_lower_access(s) for s in segments]
    assert [(p.seg, p.seg_o, p.seg_n, p.seq_zero, p.akf, p.aid) for p in parsed] == [
        (True, 0, 2, 0x1FFF, 1, 0x26),
        (True, 1, 2, 0x1FFF, 1, 0x26),
        (True, 2, 2, 0x1FFF, 1, 0x26),
    ]
    assert b"".join(p.data for p in parsed) == bytes(range(30))


def test_segmented_too_long_raises():
    with pytest.raises(ValueError):
        segmented_access(bytes(SEGMENT_SIZE * 33), akf=1, aid=0, szmic=0, seq_zero=0)


@pytest.mark.parametrize(
    "pdu",
    [b"", b"\x66\x01\x02\x03\x04", b"\x80\x00\x00", bytes.fromhex("802af0e1") + b"\x00"],
)
def test_parse_lower_access_malformed(pdu):
    with pytest.raises(MalformedPDUError):
        parse_lower_access(pdu)


@pytest.mark.parametrize(
    ("net_seq", "seq_zero", "expected"),
    [
        (0x001005, 0x1000, 0x001000),
        (0x002003, 0x1FFE, 0x001FFE),
        (0x002000, 0x0000, 0x002000),
        (5, 0x1FFE, None),
    ],
)
def test_seq_auth_from_segment(net_seq, seq_zero, expected):
    assert seq_auth_from_segment(net_seq, seq_zero) == expected
