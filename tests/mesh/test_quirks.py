import pytest

from mesh.quirks import CID_TUYA, QUIRKS, Quirk, is_vendor_opcode, quirks_for


def test_tuya_wildcard_covers_every_pid():
    for pid in (0x0280, 0x0001, None):
        flags = quirks_for(CID_TUYA, pid)
        assert Quirk.OFF_VIA_ONOFF in flags
        assert Quirk.ON_BEFORE_SET in flags
        assert Quirk.IGNORE_VENDOR_OPCODES in flags


def test_unknown_vendor_and_missing_cid_follow_the_spec():
    assert quirks_for(0x0059, 0x0001) is Quirk.NONE
    assert quirks_for(None, None) is Quirk.NONE


def test_exact_pid_entry_beats_the_wildcard(monkeypatch):
    monkeypatch.setitem(QUIRKS, (CID_TUYA, 0x1234), Quirk.OFF_VIA_ONOFF)
    assert quirks_for(CID_TUYA, 0x1234) == Quirk.OFF_VIA_ONOFF
    assert Quirk.ON_BEFORE_SET in quirks_for(CID_TUYA, 0x0280)


@pytest.mark.parametrize(
    ("opcode", "vendor"), [(0x02, False), (0x8204, False), (0xC107D0, True), (0xFFFFFF, True)]
)
def test_vendor_opcodes_are_three_bytes(opcode, vendor):
    assert is_vendor_opcode(opcode) is vendor
