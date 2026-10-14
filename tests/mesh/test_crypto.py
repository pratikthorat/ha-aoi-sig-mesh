import pytest

from mesh.crypto import aes_cmac, aes_ecb
from mesh.errors import MeshCryptoError

RFC4493_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
RFC4493_MSG = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710"
)


def test_aes_ecb_fips197():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    block = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert aes_ecb(key, block) == bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (0, "bb1d6929e95937287fa37d129b756746"),
        (16, "070a16b46b4d4144f79bdd9dd04a287c"),
        (40, "dfa66747de9ae63030ca32611497c827"),
        (64, "51f0bebf7e3b9d92fc49741779363cfe"),
    ],
)
def test_aes_cmac_rfc4493(length, expected):
    assert aes_cmac(RFC4493_KEY, RFC4493_MSG[:length]) == bytes.fromhex(expected)


def test_bad_key_length_raises():
    with pytest.raises(MeshCryptoError):
        aes_ecb(bytes(15), bytes(16))
    with pytest.raises(MeshCryptoError):
        aes_cmac(bytes(17), b"x")


def test_ecb_needs_one_block():
    with pytest.raises(MeshCryptoError):
        aes_ecb(bytes(16), bytes(15))
