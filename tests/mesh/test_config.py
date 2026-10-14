import pytest

from mesh.config import (
    CompositionData,
    Element,
    config_appkey_add,
    config_composition_get,
    config_model_app_bind,
    config_model_publication_set,
    config_node_reset,
    encode_composition,
    encode_model_id,
    pack_key_indexes,
    parse_composition_status,
    parse_status_code,
)
from mesh.errors import MalformedPDUError

APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
VENDOR_MODEL = (0x07D0 << 16) | 0x0004


def test_encode_model_id():
    assert encode_model_id(0x1000) == bytes.fromhex("0010")
    assert encode_model_id(VENDOR_MODEL) == bytes.fromhex("d0070400")


def test_pack_key_indexes():
    assert pack_key_indexes(0, 0) == bytes.fromhex("000000")
    assert pack_key_indexes(1, 2) == bytes.fromhex("012000")
    with pytest.raises(ValueError):
        pack_key_indexes(0x1000, 0)


def test_composition_get():
    assert config_composition_get() == bytes.fromhex("800800")
    assert config_composition_get(0x80) == bytes.fromhex("800880")


def test_appkey_add():
    assert config_appkey_add(0, 0, APP_KEY) == bytes.fromhex("00000000") + APP_KEY
    assert len(config_appkey_add(0, 0, APP_KEY)) == 20  # always segmented
    with pytest.raises(ValueError):
        config_appkey_add(0, 0, APP_KEY[:15])


def test_model_app_bind():
    assert config_model_app_bind(0x00B0, 0, 0x1000) == bytes.fromhex("803db00000000010")
    assert config_model_app_bind(0x00B1, 0, VENDOR_MODEL) == bytes.fromhex("803db1000000d0070400")


def test_model_publication_set():
    assert config_model_publication_set(0x00B0, 0x0001, 0, 0x1300) == bytes.fromhex(
        "03b00001000000ff00000013"
    )
    message = config_model_publication_set(
        0x00B0,
        0x0001,
        0,
        0x1300,
        ttl=5,
        period=0x41,
        retransmit_count=2,
        retransmit_interval_steps=3,
        credential=True,
    )
    assert message == bytes.fromhex("03b0000100001005411a0013")


def test_node_reset():
    assert config_node_reset() == bytes.fromhex("8049")


def test_parse_status_code():
    assert parse_status_code(b"\x00\x01\x02") == 0
    with pytest.raises(MalformedPDUError):
        parse_status_code(b"")


# Page 0 of a Tuya-style CTL bulb: element 0 has Config, Health, OnOff, Lightness, CTL and a
# Tuya vendor model; element 1 has CTL Temperature.
TUYA_CTL_COMPOSITION = bytes.fromhex(
    "00d007800237370a0003000000050100000200001000130313d0070400000001000613"
)
TUYA_CTL = CompositionData(
    page=0,
    cid=0x07D0,
    pid=0x0280,
    vid=0x3737,
    crpl=10,
    features=3,
    elements=(
        Element(0, (0x0000, 0x0002, 0x1000, 0x1300, 0x1303), (VENDOR_MODEL,)),
        Element(0, (0x1306,), ()),
    ),
)


def test_parse_composition_status():
    assert parse_composition_status(TUYA_CTL_COMPOSITION) == TUYA_CTL


def test_encode_composition_round_trip():
    assert encode_composition(TUYA_CTL) == TUYA_CTL_COMPOSITION


@pytest.mark.parametrize(
    "params",
    [
        TUYA_CTL_COMPOSITION[:10],  # header cut short
        TUYA_CTL_COMPOSITION[:11],  # header only, no elements
        TUYA_CTL_COMPOSITION[:14],  # element header cut short
        TUYA_CTL_COMPOSITION[:-1],  # model list cut short
    ],
)
def test_parse_composition_status_malformed(params):
    with pytest.raises(MalformedPDUError):
        parse_composition_status(params)
