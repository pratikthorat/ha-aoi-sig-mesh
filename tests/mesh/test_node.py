import json

import pytest

from mesh.config import CompositionData, Element
from mesh.node import MeshNode, locate_models

DEV_KEY = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")
VENDOR_MODEL = (0x07D0 << 16) | 0x0004


def composition(*elements):
    return CompositionData(0, 0x07D0, 0x0280, 0x3737, 10, 3, tuple(elements))


TUYA_CTL = composition(
    Element(0, (0x0000, 0x0002, 0x1000, 0x1300, 0x1303), (VENDOR_MODEL,)),
    Element(0, (0x1306,), ()),
)
PLUG = composition(Element(0, (0x0000, 0x1000), ()))
DIMMER = composition(Element(0, (0x0000, 0x0002, 0x1000, 0x1300), ()))
TWO_GANG = composition(Element(0, (0x0000, 0x1000), ()), Element(0, (0x1000,), ()))


def test_locate_models_tuya_ctl():
    assert locate_models(TUYA_CTL, 0x00B0) == {
        "health": 0x00B0,
        "onoff": 0x00B0,
        "lightness": 0x00B0,
        "ctl": 0x00B0,
        "ctl_temp": 0x00B1,
    }


def test_locate_models_first_element_wins():
    assert locate_models(TWO_GANG, 0x0010) == {"onoff": 0x0010}


def test_apply_composition():
    node = MeshNode(unicast=0x00B0, element_count=1, dev_key=DEV_KEY)
    node.apply_composition(TUYA_CTL)
    assert (node.element_count, node.cid, node.pid, node.vid) == (2, 0x07D0, 0x0280, 0x3737)
    assert node.is_light
    assert node.is_tunable
    assert node.contains(0x00B1)
    assert not node.contains(0x00B2)
    assert not node.contains(0x00AF)


def test_plug_is_not_a_light():
    node = MeshNode(unicast=0x0010, element_count=1, dev_key=DEV_KEY)
    node.apply_composition(PLUG)
    assert (node.is_light, node.is_tunable) == (False, False)
    assert node.models == {"onoff": 0x0010}


def test_dimmer_is_a_light_but_not_tunable():
    node = MeshNode(unicast=0x0010, element_count=1, dev_key=DEV_KEY)
    node.apply_composition(DIMMER)
    assert (node.is_light, node.is_tunable) == (True, False)


def test_node_round_trips_through_a_dict():
    node = MeshNode(unicast=0x00B0, element_count=1, dev_key=DEV_KEY, kelvin_min=3000)
    node.apply_composition(TUYA_CTL)
    data = json.loads(json.dumps(node.to_dict()))
    assert data["dev_key"] == DEV_KEY.hex()
    assert MeshNode.from_dict(data) == node


def test_node_repr_hides_dev_key():
    assert DEV_KEY.hex() not in repr(MeshNode(unicast=0x00B0, element_count=1, dev_key=DEV_KEY))


def test_node_validates():
    with pytest.raises(ValueError):
        MeshNode(unicast=0x00B0, element_count=1, dev_key=DEV_KEY[:15])
    with pytest.raises(ValueError):
        MeshNode(unicast=0x0000, element_count=1, dev_key=DEV_KEY)
    with pytest.raises(ValueError):
        MeshNode(unicast=0x7FFF, element_count=2, dev_key=DEV_KEY)
    with pytest.raises(ValueError):
        MeshNode(unicast=0x00B0, element_count=0, dev_key=DEV_KEY)
