"""async_provision_device: PB-GATT into one of our networks, with the address reserved first."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh.mesh import provisioner
from custom_components.aoi_sig_mesh.provision import (
    ProvisionFailed,
    async_provision_device,
    async_unprovisioned,
)
from custom_components.aoi_sig_mesh.store import AddressBook
from tests.sim.mesh_sim import SimMesh
from tests.sim.prov_sim import SimUnprovisioned
from tests.sim.sim_radio import SimRadio

from .conftest import NETWORK_ID, make_entry

PLUG_MAC = "DC:23:50:00:00:27"


@pytest.fixture(autouse=True)
def fast_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CAPABILITIES_TIMEOUT", "PUBLIC_KEY_TIMEOUT", "COMPLETE_TIMEOUT"):
        monkeypatch.setattr(provisioner, name, 0.2)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


async def provision(hass: HomeAssistant, radio: SimRadio, *, taken=((0x0001, 1),)):
    (device,) = async_unprovisioned(hass)
    return await async_provision_device(
        hass, make_entry().data, device, iv_index=0, taken=list(taken)
    )


async def test_provisions_at_the_first_free_address_and_reserves_it(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, kind="ctl"))

    result = await provision(hass, radio, taken=[(0x0001, 1), (0x0002, 1)])

    assert (result.unicast, result.element_count) == (0x0003, 2)
    assert sim.nodes[0x0003].dev_key == result.dev_key
    assert await AddressBook(hass).async_used_nodes(NETWORK_ID) == [(0x0003, 2)]


async def test_a_reserved_address_is_never_given_again(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """A device removed or half provisioned keeps its address: its old seq may be cached."""
    await AddressBook(hass).async_reserve_node(NETWORK_ID, 1, taken=[], low=2, high=2)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC))

    result = await provision(hass, radio)

    assert result.unicast == 0x0003


async def test_a_device_that_never_completes_is_half_provisioned_and_keeps_its_address(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, stall_at="complete"))

    with pytest.raises(ProvisionFailed) as caught:
        await provision(hass, radio)

    assert (caught.value.reason, caught.value.step) == ("half_provisioned", "complete")
    assert await AddressBook(hass).async_used_nodes(NETWORK_ID) == [(0x0002, 1)]


@pytest.mark.parametrize(
    ("switch", "reason", "step"),
    [
        ({"refuse": True}, "provision_failed", "capabilities"),
        ({"stall_at": "public_key"}, "provision_failed", "public_key"),
        ({"bad_confirmation": True}, "provision_failed", "random"),
        ({"oob_only": True}, "oob_required", "capabilities"),
    ],
)
async def test_failures_before_the_data_are_typed(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, switch, reason, step
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, **switch))

    with pytest.raises(ProvisionFailed) as caught:
        await provision(hass, radio)

    assert (caught.value.reason, caught.value.step) == (reason, step)
    assert sim.nodes == {}


async def test_a_failure_through_a_remote_proxy_suggests_the_local_adapter(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, stall_at="public_key"), remote=True)

    with pytest.raises(ProvisionFailed) as caught:
        await provision(hass, radio)

    assert caught.value.reason == "provision_failed_remote"


async def test_an_unreachable_device_fails_at_connect(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC))
    radio.unreachable.add(PLUG_MAC)

    with pytest.raises(ProvisionFailed) as caught:
        await provision(hass, radio)

    assert (caught.value.reason, caught.value.step) == ("provision_failed", "connect")
    assert await AddressBook(hass).async_used_nodes(NETWORK_ID) == []


async def test_a_full_range_is_no_address_and_the_device_is_untouched(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    device = SimUnprovisioned(PLUG_MAC)
    radio.advertise_unprovisioned(device)

    with pytest.raises(ProvisionFailed) as caught:
        await provision(hass, radio, taken=[(0x0001, 0x7F00)])

    assert caught.value.reason == "no_address"
    assert device.provisioned is None
    assert len(async_unprovisioned(hass)) == 1
