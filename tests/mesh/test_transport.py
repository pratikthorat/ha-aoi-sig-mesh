import pytest

from mesh.errors import MalformedPDUError
from mesh.transport import (
    SEGMENT_SIZE,
    LowerAccess,
    Reassembled,
    SegmentAck,
    SegmentReassembler,
    decrypt_upper,
    encrypt_upper,
    make_segment_ack,
    parse_lower_access,
    parse_segment_ack,
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


def test_make_segment_ack():
    assert make_segment_ack(0x0ABC, 0b11) == bytes.fromhex("002af000000003")
    assert make_segment_ack(0x0ABC, 0b11, obo=True) == bytes.fromhex("00aaf000000003")


def test_parse_segment_ack_round_trip():
    ack = parse_segment_ack(make_segment_ack(0x1FFF, 0xFFFFFFFF, obo=True))
    assert ack == SegmentAck(True, 0x1FFF, 0xFFFFFFFF)


@pytest.mark.parametrize(
    "pdu",
    [
        b"",
        bytes.fromhex("002af0000000"),
        bytes.fromhex("012af000000003"),
        bytes.fromhex("802af000000003"),
    ],
)
def test_parse_segment_ack_rejects(pdu):
    assert parse_segment_ack(pdu) is None


UPPER = bytes(range(30))  # 3 segments: 12 + 12 + 6


def _segments(seq_auth, upper=UPPER):
    pdus = segmented_access(upper, akf=1, aid=0x26, szmic=0, seq_zero=seq_auth & 0x1FFF)
    return [(seq_auth + i, parse_lower_access(p)) for i, p in enumerate(pdus)]


def test_reassembles_in_order():
    rx = SegmentReassembler()
    segs = _segments(100)
    first = rx.add(0x00B0, *segs[0])
    assert (first.message, first.block_ack, first.send_ack) == (None, 0b001, False)
    assert rx.add(0x00B0, *segs[1]).block_ack == 0b011
    done = rx.add(0x00B0, *segs[2])
    assert done.message == Reassembled(100, 1, 0x26, 0, UPPER)
    assert (done.block_ack, done.seq_zero, done.send_ack) == (0b111, 100, True)


def test_reassembles_out_of_order():
    rx = SegmentReassembler()
    segs = _segments(100)
    rx.add(0x00B0, *segs[2])
    rx.add(0x00B0, *segs[0])
    assert rx.add(0x00B0, *segs[1]).message.upper == UPPER


def test_retransmitted_segments_with_new_seqs_complete_the_message():
    rx = SegmentReassembler()
    segs = _segments(100)
    rx.add(0x00B0, *segs[0])
    rx.add(0x00B0, *segs[1])
    late = segs[2][1]
    assert rx.add(0x00B0, 150, late).message.seq_auth == 100  # resent later with seq 150


def test_duplicate_after_completion_is_reacked_not_redelivered():
    rx = SegmentReassembler()
    for seg in _segments(100):
        rx.add(0x00B0, *seg)
    again = rx.add(0x00B0, *_segments(100)[1])
    assert (again.message, again.block_ack, again.send_ack) == (None, 0b111, True)


def test_older_message_is_ignored():
    rx = SegmentReassembler()
    for seg in _segments(200):
        rx.add(0x00B0, *seg)
    old = rx.add(0x00B0, *_segments(100)[0])
    assert (old.message, old.block_ack, old.send_ack) == (None, 0, False)


def test_newer_message_replaces_unfinished_one():
    rx = SegmentReassembler()
    rx.add(0x00B0, *_segments(100)[0])
    segs = _segments(300)
    for seg in segs[:-1]:
        rx.add(0x00B0, *seg)
    assert rx.add(0x00B0, *segs[-1]).message.seq_auth == 300
    stale = rx.add(0x00B0, *_segments(100)[1])  # a straggler from the abandoned message
    assert (stale.message, stale.send_ack) == (None, False)


def test_sources_are_independent():
    rx = SegmentReassembler()
    a, b = _segments(100), _segments(100)
    rx.add(0x00B0, *a[0])
    rx.add(0x00C0, *b[0])
    rx.add(0x00B0, *a[1])
    assert rx.add(0x00B0, *a[2]).message is not None
    assert rx.add(0x00C0, *b[1]).message is None


def test_inconsistent_segments_are_ignored():
    rx = SegmentReassembler()
    rx.add(0x00B0, *_segments(100)[0])
    other_shape = _segments(100, upper=bytes(40))[1]  # SegN 3 instead of 2
    assert rx.add(0x00B0, *other_shape).block_ack == 0
    short = LowerAccess(True, 1, 0x26, 0, 100, 1, 2, b"\x00" * 5)  # middle segment, not 12 bytes
    assert rx.add(0x00B0, 101, short).block_ack == 0


def test_unrecoverable_seq_auth_is_ignored():
    seg = parse_lower_access(segmented_access(UPPER, akf=1, aid=0, szmic=0, seq_zero=0x1FFE)[0])
    result = SegmentReassembler().add(0x00B0, 5, seg)
    assert (result.message, result.send_ack) == (None, False)
