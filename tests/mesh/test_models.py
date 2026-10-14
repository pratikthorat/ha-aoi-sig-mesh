import pytest

from mesh.errors import MalformedPDUError
from mesh.models import (
    CTLStatus,
    CTLTemperatureRangeStatus,
    CTLTemperatureStatus,
    LightnessStatus,
    OnOffStatus,
    ctl_get,
    ctl_set,
    ctl_temperature_get,
    ctl_temperature_range_get,
    ctl_temperature_set,
    decode_transition,
    encode_opcode,
    encode_transition,
    health_attention_set,
    lightness_get,
    lightness_set,
    onoff_get,
    onoff_set,
    parse_attention_status,
    parse_ctl_status,
    parse_ctl_temperature_range_status,
    parse_ctl_temperature_status,
    parse_lightness_status,
    parse_onoff_status,
    parse_opcode,
)


@pytest.mark.parametrize(
    ("opcode", "encoded"),
    [(0x00, "00"), (0x02, "02"), (0x8204, "8204"), (0xC1D007, "c1d007")],
)
def test_encode_opcode(opcode, encoded):
    assert encode_opcode(opcode) == bytes.fromhex(encoded)


@pytest.mark.parametrize("opcode", [0x7F, 0x80, 0xC000, 0x1000000, -1])
def test_encode_opcode_rejects_invalid(opcode):
    with pytest.raises(ValueError):
        encode_opcode(opcode)


@pytest.mark.parametrize(
    ("access", "opcode", "params"),
    [
        ("820401", 0x8204, "01"),
        ("02aa", 0x02, "aa"),
        ("c1d00701", 0xC1D007, "01"),
        ("8201", 0x8201, ""),
    ],
)
def test_parse_opcode(access, opcode, params):
    assert parse_opcode(bytes.fromhex(access)) == (opcode, bytes.fromhex(params))


@pytest.mark.parametrize("access", ["", "7f00", "82", "c1d0"])
def test_parse_opcode_malformed(access):
    with pytest.raises(MalformedPDUError):
        parse_opcode(bytes.fromhex(access))


@pytest.mark.parametrize(
    ("ms", "encoded"),
    [
        (0, 0x00),
        (-5, 0x00),
        (100, 0x01),
        (150, 0x02),  # rounds up
        (6_200, 0x3E),
        (6_300, 0x47),  # 7 x 1 s
        (62_000, 0x7E),
        (63_000, 0x87),  # 7 x 10 s
        (620_000, 0xBE),
        (630_000, 0xC2),  # 2 x 10 min
        (10**9, 0xFE),  # clamps to the maximum
    ],
)
def test_encode_transition(ms, encoded):
    assert encode_transition(ms) == encoded


@pytest.mark.parametrize(
    ("value", "ms"),
    [(0x00, 0), (0x0A, 1_000), (0x47, 7_000), (0x87, 70_000), (0xC2, 1_200_000), (0x3F, None)],
)
def test_decode_transition(value, ms):
    assert decode_transition(value) == ms


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (onoff_get(), "8201"),
        (onoff_set(True, 5), "82020105"),
        (onoff_set(False, 0x105), "82020005"),  # TID wraps to one byte
        (onoff_set(True, 5, transition_ms=1_000, delay_ms=50), "820201050a0a"),
        (onoff_set(True, 5, ack=False), "82030105"),
        (lightness_get(), "824b"),
        (lightness_set(0x8000, 1), "824c008001"),
        (lightness_set(0, 1, ack=False), "824d000001"),
        (ctl_get(), "825d"),
        (ctl_set(0xFFFF, 4000, 2), "825effffa00f000002"),
        (ctl_set(0x1000, 2700, 2, delta_uv=-1, ack=False), "825f00108c0affff02"),
        (ctl_temperature_get(), "8261"),
        (ctl_temperature_set(2700, 3), "82648c0a000003"),
        (ctl_temperature_set(6500, 3, transition_ms=200, ack=False), "826564190000030200"),
        (ctl_temperature_range_get(), "8262"),
        (health_attention_set(5), "800605"),
        (health_attention_set(5, ack=True), "800505"),
        (health_attention_set(300), "8006ff"),
    ],
)
def test_message_builders(message, expected):
    assert message == bytes.fromhex(expected)


def test_builders_validate_ranges():
    with pytest.raises(ValueError):
        ctl_temperature_set(799, 1)
    with pytest.raises(ValueError):
        ctl_set(0xFFFF, 20_001, 1)
    with pytest.raises(ValueError):
        lightness_set(70_000, 1)
    with pytest.raises(ValueError):
        lightness_set(-1, 1)


def test_parse_onoff_status():
    assert parse_onoff_status(b"\x01") == OnOffStatus(True, None, None)
    assert parse_onoff_status(b"\x00\x01\x0a") == OnOffStatus(False, True, 1_000)


def test_parse_lightness_status():
    assert parse_lightness_status(bytes.fromhex("0080")) == LightnessStatus(0x8000, None, None)
    assert parse_lightness_status(bytes.fromhex("0080ffff0a")) == LightnessStatus(
        0x8000, 0xFFFF, 1_000
    )


def test_parse_ctl_status():
    assert parse_ctl_status(bytes.fromhex("ffffa00f")) == CTLStatus(0xFFFF, 4000, None, None, None)
    assert parse_ctl_status(bytes.fromhex("ffffa00f00108c0a3f")) == CTLStatus(
        0xFFFF, 4000, 0x1000, 2700, None
    )


def test_parse_ctl_temperature_status():
    assert parse_ctl_temperature_status(bytes.fromhex("8c0affff")) == CTLTemperatureStatus(
        2700, -1, None, None, None
    )
    assert parse_ctl_temperature_status(bytes.fromhex("8c0a0000641900000a")) == (
        CTLTemperatureStatus(2700, 0, 6500, 0, 1_000)
    )


def test_parse_ctl_temperature_range_status():
    assert parse_ctl_temperature_range_status(bytes.fromhex("008c0a6419")) == (
        CTLTemperatureRangeStatus(0, 2700, 6500)
    )


def test_parse_attention_status():
    assert parse_attention_status(b"\x05") == 5


@pytest.mark.parametrize(
    ("parser", "params"),
    [
        (parse_onoff_status, ""),
        (parse_onoff_status, "0001"),
        (parse_lightness_status, "00"),
        (parse_lightness_status, "00800080"),
        (parse_ctl_status, "ffffa0"),
        (parse_ctl_temperature_status, "8c0a00000000"),
        (parse_ctl_temperature_range_status, "008c0a64"),
        (parse_attention_status, ""),
    ],
)
def test_status_parsers_reject_bad_lengths(parser, params):
    with pytest.raises(MalformedPDUError):
        parser(bytes.fromhex(params))
