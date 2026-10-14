import pytest

from mesh.errors import MalformedPDUError
from mesh.models import encode_opcode, parse_opcode


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
