"""nRF Mesh JSON (Mesh Configuration Database) import."""

import copy
import json

import pytest

from mesh.importers import NrfImportError, import_nrf_json
from tests.sim.mesh_sim import APP_KEY, NET_KEY

PHONE_UUID = "0E3CB9F1-7E0C-4E06-9A40-62C2E1A0B6F1"
BULB_KEY = bytes(range(16))
PLUG_KEY = bytes(range(16, 32))


def element(index, *models, bind=(0,)):
    return {
        "name": f"Element {index}",
        "index": index,
        "location": "0000",
        "models": [{"modelId": m, "bind": list(bind), "subscribe": []} for m in models],
    }


def export():
    """An nRF Mesh export: the phone (a provisioner and a node), a CTL bulb and a plug."""
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "version": "1.0.1",
        "meshUUID": "6F1A0F44-7E45-4B6A-9F5C-0C1D6E1C1A00",
        "meshName": "Home",
        "partial": False,
        "provisioners": [
            {
                "provisionerName": "Phone",
                "UUID": PHONE_UUID,
                "allocatedUnicastRange": [{"lowAddress": "0001", "highAddress": "199A"}],
                "allocatedGroupRange": [{"lowAddress": "C000", "highAddress": "CC9A"}],
                "allocatedSceneRange": [{"firstScene": "0001", "lastScene": "3333"}],
            }
        ],
        "netKeys": [{"name": "Primary", "index": 0, "key": NET_KEY.hex().upper(), "phase": 0}],
        "appKeys": [
            {"name": "Other", "index": 3, "boundNetKey": 1, "key": "00" * 16},
            {"name": "Home", "index": 1, "boundNetKey": 0, "key": APP_KEY.hex()},
        ],
        "nodes": [
            {
                "UUID": PHONE_UUID,
                "name": "Phone",
                "unicastAddress": "0001",
                "deviceKey": "AA" * 16,
                "elements": [element(0, "0000", "0001", "0002", "0003", bind=())],
                "excluded": False,
            },
            {
                "UUID": "A1",
                "name": "Desk lamp",
                "unicastAddress": "0002",
                "deviceKey": BULB_KEY.hex(),
                "cid": "07D0",
                "pid": "0280",
                "vid": "3737",
                "elements": [
                    element(0, "0000", "0002", "1000", "1300", "1303", "07D00004", bind=(1,)),
                    element(1, "1306", bind=(1,)),
                ],
                "excluded": False,
            },
            {
                "UUID": "A2",
                "name": "Kettle plug",
                "unicastAddress": "0004",
                "deviceKey": PLUG_KEY.hex(),
                "cid": "07D0",
                "pid": "0001",
                "vid": "0001",
                "elements": [element(0, "0000", "1000", bind=())],
                "excluded": False,
            },
        ],
        "groups": [],
        "scenes": [],
        "networkExclusions": [],
    }


def load(data, **kwargs):
    return import_nrf_json(json.dumps(data), **kwargs)


def test_imports_keys_nodes_and_a_fresh_address():
    result = load(export())
    assert result.title == "Home"
    assert (result.net_key, result.net_key_index) == (NET_KEY, 0)
    assert (result.app_key, result.app_key_index) == (APP_KEY, 1)
    assert result.iv_index == 0
    assert result.address == 0x7F00
    assert (result.range_low, result.range_high) == (0x199B, 0x7EFF)
    assert [n.title for n in result.nodes] == ["Desk lamp", "Kettle plug"]
    assert result.skipped == 0
    lamp, plug = result.nodes
    assert (lamp.node.unicast, lamp.node.element_count, lamp.node.dev_key) == (2, 2, BULB_KEY)
    assert (lamp.node.cid, lamp.node.pid, lamp.node.vid) == (0x07D0, 0x0280, 0x3737)
    assert lamp.node.models == {
        "health": 2,
        "onoff": 2,
        "lightness": 2,
        "ctl": 2,
        "ctl_temp": 3,
    }
    assert lamp.configured is True  # every role bound to our AppKey (index 1)
    assert plug.node.models == {"onoff": 4}
    assert plug.configured is False  # OnOff not bound yet: first connect sets it up
    assert all(n.mac is None for n in result.nodes)
    # the phone and every device hold their addresses, imported or not
    assert result.occupied == [(0x0001, 1), (0x0002, 2), (0x0004, 1)]


def test_keys_never_show_in_repr():
    text = repr(load(export()))
    for key in (NET_KEY, APP_KEY, BULB_KEY, PLUG_KEY):
        assert key.hex() not in text.lower()


def test_our_address_avoids_every_provisioner_range_node_exclusion_and_known_address():
    data = export()
    data["provisioners"].append(
        {
            "provisionerName": "Tablet",
            "UUID": "B0",
            "allocatedUnicastRange": [{"lowAddress": "7F00", "highAddress": "7F7F"}],
        }
    )
    data["nodes"][2]["unicastAddress"] = "7F80"
    data["networkExclusions"] = [{"ivIndex": 0, "addresses": ["7F81"]}]
    result = load(data, used_addresses=[(0x7F82, 1)])
    assert result.address == 0x7F83
    assert (0x7F80, 1) in result.occupied and (0x7F81, 1) in result.occupied
    assert (0x7F82, 1) not in result.occupied  # our own old address is not a device
    # devices we add go in the largest gap no provisioner owns
    assert (result.range_low, result.range_high) == (0x199B, 0x7EFF)


def test_a_full_address_space_is_no_address():
    data = export()
    data["provisioners"][0]["allocatedUnicastRange"] = [
        {"lowAddress": "0001", "highAddress": "7FFF"}
    ]
    with pytest.raises(NrfImportError) as caught:
        load(data)
    assert caught.value.reason == "no_address"


def test_iv_index_is_read_when_the_export_has_it():
    data = export()
    data["ivIndex"] = 5
    assert load(data).iv_index == 5
    data["ivIndex"] = {"index": 6, "updateActive": False}
    assert load(data).iv_index == 6


def test_unusable_nodes_are_skipped_and_counted():
    data = export()
    nodes = data["nodes"]
    nodes.append(
        {**copy.deepcopy(nodes[2]), "UUID": "A3", "unicastAddress": "0005", "excluded": True}
    )
    nodes.append(
        {**copy.deepcopy(nodes[2]), "UUID": "A4", "unicastAddress": "0006", "deviceKey": "zz"}
    )
    sensor = copy.deepcopy(nodes[2])
    sensor.update(UUID="A5", unicastAddress="0007", elements=[element(0, "0000", "1100")])
    nodes.append(sensor)
    result = load(data)
    assert [n.title for n in result.nodes] == ["Desk lamp", "Kettle plug"]
    assert result.skipped == 3


def test_a_node_without_composition_is_imported_for_setup():
    data = export()
    data["nodes"][2]["elements"] = []
    data["nodes"][2].pop("name")
    plug = load(data).nodes[1]
    assert plug.title == "Node 0x0004"
    assert (plug.node.element_count, plug.node.models) == (1, {"onoff": 4})
    assert plug.configured is False


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d.pop("netKeys"), "bad_keys"),
        (lambda d: d["netKeys"][0].update(key="1234"), "bad_keys"),
        (lambda d: d["netKeys"][0].update(phase=1), "key_refresh"),
        (lambda d: d["appKeys"].pop(1), "no_app_key"),
        (lambda d: d["appKeys"][1].update(key="nothex"), "bad_keys"),
        (lambda d: d["nodes"][1].update(unicastAddress="8001"), "bad_address"),
        (lambda d: d["nodes"][1].update(unicastAddress=17), "bad_address"),
        (
            lambda d: d["provisioners"][0]["allocatedUnicastRange"][0].update(lowAddress="x"),
            "bad_address",
        ),
        (lambda d: d.update(ivIndex=-1), "bad_iv_index"),
        (lambda d: d.update(ivIndex=True), "bad_iv_index"),
        (lambda d: d.update(nodes="none"), "bad_json"),
    ],
)
def test_invalid_exports_raise_typed_errors(change, reason):
    data = export()
    change(data)
    with pytest.raises(NrfImportError) as caught:
        load(data)
    assert caught.value.reason == reason


@pytest.mark.parametrize("text", ["", "{", "[]", "null", '"text"'])
def test_text_that_is_not_a_json_object_is_bad_json(text):
    with pytest.raises(NrfImportError) as caught:
        import_nrf_json(text)
    assert caught.value.reason == "bad_json"


def test_error_messages_never_carry_keys():
    data = export()
    data["netKeys"][0]["key"] = NET_KEY.hex()[:30]
    with pytest.raises(NrfImportError) as caught:
        load(data)
    assert NET_KEY.hex()[:30] not in str(caught.value).lower()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d["nodes"][1]["elements"][0].update(models=5), "bad_json"),
        (lambda d: d["nodes"][1]["elements"][0].update(models=["0000"]), "bad_json"),
        (lambda d: d["provisioners"][0].update(allocatedUnicastRange=5), "bad_json"),
        (lambda d: d["provisioners"][0].update(allocatedUnicastRange=["0001"]), "bad_json"),
        (lambda d: d.update(networkExclusions=5), "bad_json"),
        (lambda d: d.update(networkExclusions=[{"addresses": "7F81"}]), "bad_json"),
        (lambda d: d.update(networkExclusions=[{"addresses": 5}]), "bad_json"),
        (
            lambda d: d["provisioners"][0]["allocatedUnicastRange"][0].update(
                lowAddress="0010", highAddress="0001"
            ),
            "bad_address",
        ),
    ],
)
def test_malformed_shapes_raise_typed_errors(change, reason):
    data = export()
    change(data)
    with pytest.raises(NrfImportError) as caught:
        load(data)
    assert caught.value.reason == reason
