import pytest

from mesh.crypto import aes_cmac, aes_ecb, beacon_key, k1, k2, k3, k4, s1
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


SAMPLE_NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
SAMPLE_APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")


def test_s1_spec_sample():
    assert s1("test") == bytes.fromhex("b73cefbd641ef2ea598c2b6efb62f79c")
    assert s1(b"test") == s1("test")


def test_k1_spec_sample():
    n = bytes.fromhex("3216d1509884b533248541792b877f98")
    salt = bytes.fromhex("2ba14ffa0df84a2831938d57d276cab4")
    p = bytes.fromhex("5a09d60797eeb4478aada59db3352a0d")
    assert k1(n, salt, p) == bytes.fromhex("f6ed15a8934afbe7d83e8dcb57fcf5d7")


def test_k2_spec_sample():
    nid, enc, priv = k2(bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00"))
    assert nid == 0x7F
    assert enc == bytes.fromhex("9f589181a0f50de73c8070c7a6d27f46")
    assert priv == bytes.fromhex("4c715bd4a64b938f99b453351653124f")


def test_k3_spec_sample():
    assert k3(bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")) == bytes.fromhex(
        "ff046958233db014"
    )


def test_k4_spec_sample():
    assert k4(bytes.fromhex("3216d1509884b533248541792b877f98")) == 0x38


def test_sample_net_key_derivations():
    nid, enc, priv = k2(SAMPLE_NET_KEY)
    assert nid == 0x68
    assert enc == bytes.fromhex("0953fa93e7caac9638f58820220a398e")
    assert priv == bytes.fromhex("8b84eedec100067d670971dd2aa700cf")
    assert k3(SAMPLE_NET_KEY) == bytes.fromhex("3ecaff672f673370")
    assert beacon_key(SAMPLE_NET_KEY) == bytes.fromhex("5423d967da639a99cb02231a83f7d254")


def test_sample_app_key_aid():
    assert k4(SAMPLE_APP_KEY) == 0x26
