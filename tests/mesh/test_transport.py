import pytest

from mesh.transport import decrypt_upper, encrypt_upper

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
