import pytest

from mesh import proxy
from mesh.config import CompositionData, Element
from mesh.configure import ConfigureError, configure_node
from mesh.network import MeshNetwork
from mesh.node import KELVIN_DEFAULT_MAX, KELVIN_DEFAULT_MIN, MeshNode
from mesh.proxy import ProxyLink
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, run


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.2)


async def open_link(sim, address=CLIENT):
    network = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=address)
    link = ProxyLink(network, sim.connect(), dev_key_for=sim.dev_key_for)
    await link.start()
    return network, link


def bare_node(sim_node) -> MeshNode:
    """What a tuya import knows before the first connect: one element, OnOff only."""
    return MeshNode(
        unicast=sim_node.unicast,
        element_count=1,
        dev_key=sim_node.dev_key,
        models={"onoff": sim_node.unicast},
    )


def test_configure_a_fresh_ctl_bulb():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl", configured=False)
        bulb.kelvin_min, bulb.kelvin_max = 3000, 6000
        network, link = await open_link(sim, address=0x7F00)
        node = bare_node(bulb)
        result = await configure_node(link, network, node, publish=True)
        assert node.element_count == 2
        assert (node.cid, node.pid, node.vid) == (0x07D0, 0x0280, 0x3737)
        assert node.models == {
            "health": 0x00B0,
            "onoff": 0x00B0,
            "lightness": 0x00B0,
            "ctl": 0x00B0,
            "ctl_temp": 0x00B1,
        }
        assert result.bound == ["onoff", "lightness", "ctl", "ctl_temp"]
        assert result.published == result.bound
        assert bulb.has_app_key is True
        assert bulb.bindings == {
            (0x00B0, 0x1000),
            (0x00B0, 0x1300),
            (0x00B0, 0x1303),
            (0x00B1, 0x1306),
        }
        assert set(bulb.publications.values()) == {0x7F00}
        assert result.kelvin_range == (3000, 6000)
        assert (node.kelvin_min, node.kelvin_max) == (3000, 6000)

    run(scenario())


def test_configure_without_publication_and_on_a_plug():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff", configured=False)
        network, link = await open_link(sim)
        node = bare_node(plug)
        result = await configure_node(link, network, node, publish=False)
        assert result.bound == ["onoff"]
        assert result.published == []
        assert plug.publications == {}
        assert result.kelvin_range is None
        assert node.models == {"onoff": 0x00B0}

    run(scenario())


def test_configure_is_idempotent_for_an_already_configured_node():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        network, link = await open_link(sim)
        node = bare_node(bulb)
        await configure_node(link, network, node, publish=True)
        again = await configure_node(link, network, node, publish=True)
        assert again.bound == ["onoff", "lightness", "ctl", "ctl_temp"]

    run(scenario())


def test_a_silent_node_fails_at_composition():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        network, link = await open_link(sim)
        sim.silent = True
        with pytest.raises(ConfigureError) as err:
            await configure_node(link, network, bare_node(bulb), publish=True)
        assert err.value.step == "composition"

    run(scenario(), timeout=10)


def test_a_cid_0000_vendor_model_never_becomes_a_role():
    """CID 0x0000 + model 0x1300 encodes to 0x1300, the SIG Lightness Server's ID."""
    node = MeshNode(unicast=0x00C0, element_count=1, dev_key=bytes(16))
    node.apply_composition(
        CompositionData(0, 0x0000, 0, 0, 10, 3, (Element(0, (0x0000, 0x1000), (0x1300,)),))
    )
    assert node.models == {"onoff": 0x00C0}


def test_vendor_models_are_never_bound():
    async def scenario():
        sim = SimMesh()
        plug = sim.add_node("onoff")
        tuya_lightness = (0x07D0 << 16) | 0x1300  # a vendor model ending in a SIG-looking ID
        plug.composition = lambda: CompositionData(
            0, 0x07D0, 0x0280, 0x3737, 10, 3, (Element(0, (0x0000, 0x1000), (tuya_lightness,)),)
        )
        network, link = await open_link(sim)
        result = await configure_node(link, network, bare_node(plug), publish=True)
        assert result.bound == ["onoff"]
        assert plug.bindings == {(plug.unicast, 0x1000)}

    run(scenario())


def test_an_inverted_temperature_range_keeps_the_defaults():
    async def scenario():
        sim = SimMesh()
        bulb = sim.add_node("ctl")
        bulb.kelvin_min, bulb.kelvin_max = 6000, 3000
        network, link = await open_link(sim)
        node = bare_node(bulb)
        result = await configure_node(link, network, node, publish=True)
        assert result.kelvin_range is None
        assert (node.kelvin_min, node.kelvin_max) == (KELVIN_DEFAULT_MIN, KELVIN_DEFAULT_MAX)

    run(scenario())
