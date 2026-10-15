import copy

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


def _sig(**change):
    return {**TUYA_DATA, "sig_models": {**TUYA_DATA["sig_models"], **change}}


@pytest.mark.parametrize("iv", [-1, 0x1_0000_0000, 1.5, True, "0"])
def test_a_bad_iv_index_is_refused(iv):
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("Mesh CW", {**TUYA_DATA, "iv_index": iv})
    assert err.value.reason == "bad_iv_index"


def test_iv_index_limits_are_accepted():
    assert import_tuya_entry("x", {**TUYA_DATA, "iv_index": 0xFFFFFFFF}).iv_index == 0xFFFFFFFF


@pytest.mark.parametrize("addr", [0x00AF, 0x00B0 + 16, 0x7FFF + 1])
def test_role_addresses_outside_the_node_window_are_refused(addr):
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("x", _sig(ctl_temp=addr))
    assert err.value.reason == "bad_address"


def test_a_role_address_grows_the_element_count_within_the_bound():
    node = import_tuya_entry("x", _sig(elements=1, ctl_temp=0xB2)).node
    assert node.element_count == 3
    assert import_tuya_entry("x", _sig(elements=1, ctl_temp=0xB0 + 15)).node.element_count == 16


def test_ids_are_coerced_to_int_or_none():
    node = import_tuya_entry("x", _sig(cid="0x07D0", pid="junk", vid=None)).node
    assert (node.cid, node.pid, node.vid) == (0x07D0, None, None)
    node = import_tuya_entry("x", _sig(cid=True, pid=[1])).node
    assert (node.cid, node.pid) == (None, None)


def test_a_short_dev_key_is_refused():
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("x", {**TUYA_DATA, "dev_key": "00" * 8})
    assert err.value.reason == "bad_keys"


def test_missing_unicast_target_is_refused():
    data = {k: v for k, v in TUYA_DATA.items() if k != "unicast_target"}
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("x", data)
    assert err.value.reason == "bad_address"


@pytest.mark.parametrize(
    ("temp_range", "expected"),
    [
        (None, (2700, 6500)),
        ([6500, 2700], (2700, 6500)),
        ([3000, 3000], (2700, 6500)),
        (["a", "b"], (2700, 6500)),
        ([2700], (2700, 6500)),
        ([100, 30000], (800, 20000)),
        ([3000, 5000], (3000, 5000)),
    ],
)
def test_kelvin_range_fallback_and_clamp(temp_range, expected):
    node = import_tuya_entry("x", _sig(temp_range=temp_range)).node
    assert (node.kelvin_min, node.kelvin_max) == expected


def test_no_free_provisioner_address_is_refused():
    with pytest.raises(TuyaImportError) as err:
        import_tuya_entry("x", TUYA_DATA, used_addresses=[(0x7F00, 0x100)])
    assert err.value.reason == "no_address"


def test_a_node_at_the_senders_own_address_is_skipped():
    data = {k: v for k, v in TUYA_DATA.items() if k != "sig_models"}
    result = import_tuya_entry("x", {**data, "unicast_target": "7F00"})
    assert result.node.unicast == 0x7F00
    assert result.address == 0x7F01  # past the node's one element


def test_the_source_mapping_is_not_changed():
    before = copy.deepcopy(TUYA_DATA)
    import_tuya_entry("x", TUYA_DATA)
    assert before == TUYA_DATA
