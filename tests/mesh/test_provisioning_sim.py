"""Provisioning a factory-reset simulated node through SimRadio, then using it on the mesh."""

import pytest

from mesh import provisioner, proxy
from mesh.configure import configure_node
from mesh.errors import MeshError, ProvisioningError
from mesh.models import OP_ONOFF_STATUS, onoff_set
from mesh.network import MeshNetwork
from mesh.node import MeshNode, allocate_unicast
from mesh.provisioner import (
    UnprovisionedDevice,
    parse_provisioning_service_data,
    provision_device,
)
from mesh.proxy import ProxyLink, parse_proxy_service_data
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, run
from tests.sim.prov_sim import SimUnprovisioned
from tests.sim.sim_radio import SimRadio

PLUG_MAC = "DC:23:50:00:00:27"
UUID = bytes(range(16))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.2)
    for name in ("CAPABILITIES_TIMEOUT", "PUBLIC_KEY_TIMEOUT", "COMPLETE_TIMEOUT"):
        monkeypatch.setattr(provisioner, name, 0.1)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


def test_parse_provisioning_service_data():
    assert parse_provisioning_service_data(UUID + b"\x40\x01") == (UUID, 0x4001)
    assert parse_provisioning_service_data(UUID + b"\x00") is None
    assert parse_provisioning_service_data(UUID + b"\x00\x00\x00") is None
    assert parse_provisioning_service_data(b"") is None


def test_the_radio_lists_unprovisioned_devices():
    sim = SimMesh()
    radio = SimRadio(sim)
    device = SimUnprovisioned(PLUG_MAC, uuid=UUID, oob_info=0x0020, name="Plug")
    radio.advertise_unprovisioned(device, rssi=-55)
    assert radio.unprovisioned() == [
        UnprovisionedDevice(PLUG_MAC, "Plug", -55, UUID, 0x0020, remote=False)
    ]
    assert radio.proxies() == []  # 0x1827 is not a proxy advert


def test_connecting_to_a_device_that_is_not_advertising_fails():
    async def scenario():
        radio = SimRadio(SimMesh())
        with pytest.raises(ProvisioningError) as caught:
            await provision_device(
                radio, PLUG_MAC, net_key=NET_KEY, net_key_index=0, iv_index=0, assign=None
            )
        assert caught.value.step == "connect"

    run(scenario())


def assign_from(sim, network_address=CLIENT):
    async def assign(caps):
        used = [(node.unicast, node.element_count) for node in sim.nodes.values()]
        return allocate_unicast([*used, (network_address, 1)], caps.elements, low=0x0002)

    return assign


def test_a_factory_reset_plug_is_provisioned_configured_and_switched():
    async def scenario():
        sim = SimMesh()
        sim.add_node("ctl", unicast=0x0002)
        radio = SimRadio(sim)
        device = SimUnprovisioned(PLUG_MAC, kind="onoff")
        radio.advertise_unprovisioned(device)
        network = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=CLIENT)
        seq_before = network.next_seq()

        result = await provision_device(
            radio,
            PLUG_MAC,
            net_key=NET_KEY,
            net_key_index=0,
            iv_index=0,
            assign=assign_from(sim),
        )

        assert result.unicast == 0x0004  # after the bulb's two elements
        assert result.element_count == 1
        assert network.next_seq() == seq_before + 1  # provisioning took no network seq
        assert not device._transport.is_connected  # the PB-GATT link is closed afterwards
        assert radio.unprovisioned() == []
        advert = parse_proxy_service_data(radio.adverts[PLUG_MAC].service_data)
        assert advert.network_id == sim.keys.network_id
        plug = sim.nodes[0x0004]
        assert plug.dev_key == result.dev_key
        assert plug.has_app_key is False

        link = ProxyLink(network, await radio.connect(PLUG_MAC), dev_key_for=sim.dev_key_for)
        await link.start()
        node = MeshNode(unicast=result.unicast, element_count=1, dev_key=result.dev_key, models={})
        configured = await configure_node(link, network, node, publish=True)
        assert configured.bound == ["onoff"]
        reply = await link.request(node.unicast, onoff_set(True, tid=1), OP_ONOFF_STATUS)
        assert reply.params[0] == 1
        assert plug.on is True
        await link.close()

    run(scenario())


def test_a_device_that_never_says_complete_is_on_the_mesh_but_reported():
    """Half provisioned: it took our keys and address, so the address must stay reserved."""

    async def scenario():
        sim = SimMesh()
        radio = SimRadio(sim)
        device = SimUnprovisioned(PLUG_MAC, stall_at="complete")
        radio.advertise_unprovisioned(device)
        with pytest.raises(ProvisioningError) as caught:
            await provision_device(
                radio,
                PLUG_MAC,
                net_key=NET_KEY,
                net_key_index=0,
                iv_index=0,
                assign=assign_from(sim),
            )
        assert caught.value.step == "complete"
        assert 0x0002 in sim.nodes
        assert not device._transport.is_connected

    run(scenario())


def test_a_device_given_another_network_key_does_not_join_this_mesh():
    async def scenario():
        sim = SimMesh()
        radio = SimRadio(sim)
        device = SimUnprovisioned(PLUG_MAC)
        radio.advertise_unprovisioned(device)
        other = bytes(16)
        await provision_device(
            radio, PLUG_MAC, net_key=other, net_key_index=0, iv_index=0, assign=assign_0x10
        )
        assert sim.nodes == {}
        advert = parse_proxy_service_data(radio.adverts[PLUG_MAC].service_data)
        assert advert.network_id != sim.keys.network_id

    run(scenario())


async def assign_0x10(caps):
    return 0x0010


def test_an_assign_failure_closes_the_link():
    async def scenario():
        radio = SimRadio(SimMesh())
        device = SimUnprovisioned(PLUG_MAC)
        radio.advertise_unprovisioned(device)

        async def full(caps):
            raise MeshError("no free addresses")

        with pytest.raises(MeshError, match="no free addresses"):
            await provision_device(
                radio, PLUG_MAC, net_key=NET_KEY, net_key_index=0, iv_index=0, assign=full
            )
        assert not device._transport.is_connected
        assert radio.unprovisioned() != []  # still factory-reset

    run(scenario())
