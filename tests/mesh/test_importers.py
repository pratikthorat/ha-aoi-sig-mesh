import pytest

from mesh.importers import TuyaImportError, import_tuya_entry, is_tuya_sig_entry
from tests.sim.mesh_sim import APP_KEY, NET_KEY

DEV_KEY = bytes(range(16))

# The shape of this house's tuya_ble_mesh entry (keys replaced by the sim's test keys).
TUYA_DATA = {
    "device_type": "sig_plug",
    "mac_address": "dc:23:50:00:00:19",
    "unicast_target": "00B0",
    "unicast_our": "0001",
    "iv_index": 0,
    "net_key": NET_KEY.hex(),
    "app_key": APP_KEY.hex(),
    "dev_key": DEV_KEY.hex(),
    "sig_models": {
        "cid": 0x07D0,
        "pid": 0x0280,
        "vid": 0x3737,
        "elements": 2,
        "onoff": 176,
        "lightness": 176,
        "ctl": 176,
        "ctl_temp": 177,
        "bound": ["onoff", "lightness", "ctl", "ctl_temp"],
        "temp_range": [2700, 6500],
        "appkey_added": True,
    },
}


def test_imports_the_mesh_cw_bulb():
    result = import_tuya_entry("Mesh CW", TUYA_DATA)
    assert result.title == "Mesh CW"
    assert (result.net_key, result.app_key) == (NET_KEY, APP_KEY)
    assert result.iv_index == 0
    assert result.mac == "DC:23:50:00:00:19"
    assert result.old_address == 0x0001
    assert result.address == 0x7F00
    node = result.node
    assert (node.unicast, node.element_count, node.dev_key) == (0x00B0, 2, DEV_KEY)
    assert (node.cid, node.pid, node.vid) == (0x07D0, 0x0280, 0x3737)
    assert node.models == {"onoff": 0xB0, "lightness": 0xB0, "ctl": 0xB0, "ctl_temp": 0xB1}
    assert (node.kelvin_min, node.kelvin_max) == (2700, 6500)
    assert node.is_tunable


def test_keys_never_show_in_repr():
    text = repr(import_tuya_entry("Mesh CW", TUYA_DATA))
    assert NET_KEY.hex() not in text
    assert APP_KEY.hex() not in text
    assert DEV_KEY.hex() not in text


def test_a_plug_without_sig_models_gets_onoff_only():
    data = {key: value for key, value in TUYA_DATA.items() if key != "sig_models"}
    node = import_tuya_entry("Plug", data).node
    assert node.models == {"onoff": 0xB0}
    assert node.element_count == 1
    assert not node.is_light


def test_provisioner_address_skips_addresses_already_used():
    used = [(0x7F00, 1), (0x7F01, 1)]
    assert import_tuya_entry("Mesh CW", TUYA_DATA, used_addresses=used).address == 0x7F02
    data = {**TUYA_DATA, "unicast_our": "7F00"}
    assert import_tuya_entry("Mesh CW", data).address == 0x7F01


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"device_type": "sig_bridge_plug"}, "not_sig"),
        ({"device_type": "light"}, "not_sig"),
        ({"net_key": "zz"}, "bad_keys"),
        ({"app_key": "00" * 15}, "bad_keys"),
        ({"dev_key": None}, "bad_keys"),
        ({"unicast_target": "C000"}, "bad_address"),
        ({"unicast_target": "xyz"}, "bad_address"),
    ],
)
def test_bad_entries_are_refused_with_a_reason(change, reason):
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("Mesh CW", {**TUYA_DATA, **change})
    assert err.value.reason == reason


def test_is_tuya_sig_entry():
    assert is_tuya_sig_entry(TUYA_DATA)
    assert not is_tuya_sig_entry({"device_type": "plug"})
