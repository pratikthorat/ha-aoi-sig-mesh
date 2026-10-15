"""Bluetooth discovery of a factory-reset device (0x1827): add it, to a network or a new one."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_IGNORE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aoi_sig_mesh import config_flow
from custom_components.aoi_sig_mesh import provision as provision_module
from custom_components.aoi_sig_mesh.const import DOMAIN, PROVISIONING_SERVICE_UUID
from custom_components.aoi_sig_mesh.mesh import provisioner
from custom_components.aoi_sig_mesh.mesh.pdu import NetworkKeys
from custom_components.aoi_sig_mesh.store import AddressBook
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimMesh
from tests.sim.prov_sim import SimUnprovisioned
from tests.sim.sim_radio import BULB_MAC, SimRadio

from .conftest import make_entry, until

PLUG_MAC = "DC:23:50:00:00:27"


@pytest.fixture(autouse=True)
def fast_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CAPABILITIES_TIMEOUT", "PUBLIC_KEY_TIMEOUT", "COMPLETE_TIMEOUT"):
        monkeypatch.setattr(provisioner, name, 0.2)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


@pytest.fixture(autouse=True)
def sim_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new network gets the sim's keys (NetKey, then AppKey), so the sim can join it."""
    keys: Iterator[bytes] = iter([NET_KEY, APP_KEY])
    monkeypatch.setattr(config_flow, "secrets", SimpleNamespace(token_bytes=lambda n: next(keys)))


def advert(device: SimUnprovisioned, data: bytes | None = None) -> BluetoothServiceInfo:
    return BluetoothServiceInfo(
        name=device.name or "",
        address=device.address,
        rssi=-60,
        manufacturer_data={},
        service_data={PROVISIONING_SERVICE_UUID: device.service_data if data is None else data},
        service_uuids=[PROVISIONING_SERVICE_UUID],
        source="local",
    )


async def discover(hass: HomeAssistant, device: SimUnprovisioned, **kwargs: Any) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=advert(device, **kwargs)
    )


async def provision(
    hass: HomeAssistant, result: dict[str, Any], **user_input: Any
) -> dict[str, Any]:
    """Submit the device form and run provisioning to its end."""
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_bad_provisioning_service_data_is_not_supported(hass: HomeAssistant) -> None:
    result = await discover(hass, SimUnprovisioned(PLUG_MAC), data=b"\x00" * 5)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_supported")


async def test_with_no_network_the_device_gets_a_new_one(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)

    result = await discover(hass, plug)

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert [str(key) for key in result["data_schema"].schema] == ["name", "role"]
    assert result["description_placeholders"]["name"] == "Plug"
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"name": "Plug"}
    assert flow["context"]["unique_id"] == plug.uuid.hex()

    result = await provision(hass, result, name="Flat", role="auto")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Flat"
    assert entry.data["address"] == 0x0001
    assert entry.data["net_key"] == NET_KEY.hex() == plug.provisioned.net_key.hex()
    assert entry.data["app_key"] == APP_KEY.hex()
    assert plug.provisioned.iv_index == 0
    network_id = NetworkKeys.from_net_key(plug.provisioned.net_key).network_id.hex()
    assert entry.unique_id == network_id
    (subentry,) = entry.subentries.values()
    assert (subentry.title, subentry.unique_id) == ("Plug", "0002")
    assert subentry.data["configured"] is False
    assert await AddressBook(hass).async_used(network_id) == [(0x0001, 1)]
    assert await AddressBook(hass).async_used_nodes(network_id) == [(0x0002, 1)]

    await until(lambda: entry.subentries[subentry.subentry_id].data["configured"])
    await until(lambda: hass.states.get("switch.plug") is not None)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_with_a_network_the_device_joins_the_chosen_one(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    other = make_entry(network_id="0011223344556677")  # not set up: still offered
    other.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id="ab" * 16, source=SOURCE_IGNORE).add_to_hass(hass)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)

    result = await discover(hass, plug)

    schema = result["data_schema"].schema
    network_key = next(key for key in schema if str(key) == "network")
    assert [o["value"] for o in schema[network_key].config["options"]] == [
        bulb_entry.entry_id,
        other.entry_id,
    ]
    result = await provision(hass, result, network=bulb_entry.entry_id, role="switch")

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "device_added")
    assert result["description_placeholders"] == {"name": "Plug", "network": bulb_entry.title}
    added = next(s for s in bulb_entry.subentries.values() if s.title == "Plug")
    assert (added.unique_id, added.data["role"]) == ("0002", "switch")
    assert len(hass.config_entries.async_entries(DOMAIN)) == 3  # no entry was made
    await until(lambda: bulb_entry.subentries[added.subentry_id].data["configured"])
    await until(lambda: hass.states.get("switch.plug") is not None)


async def test_adding_a_discovered_device_aborts_the_other_card_for_it(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    """The card was raised from an older advert; the device now advertises a new UUID, which
    a second card (another address) carries. Two cards can't share a UUID, so this is the only
    way to have a stale one on the discovery path."""
    old = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(old)
    card = await discover(hass, old)
    new = SimUnprovisioned(PLUG_MAC, name="Plug")  # same address, new UUID
    radio.advertise_unprovisioned(new)
    stale = SimUnprovisioned("DC:23:50:00:00:29", uuid=new.uuid, name="Twin")
    radio.advertise_unprovisioned(stale)
    other = await discover(hass, stale)
    unrelated = SimUnprovisioned("DC:23:50:00:00:30", name="Lamp")
    radio.advertise_unprovisioned(unrelated)
    keep = await discover(hass, unrelated)

    result = await provision(hass, card, network=bulb_entry.entry_id, role="auto")

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "device_added")
    left = [f["flow_id"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)]
    assert left == [keep["flow_id"]]
    assert other["flow_id"] not in left
    await hass.async_block_till_done()


async def test_a_failure_shows_on_the_device_form_and_a_retry_keeps_the_new_network(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="public_key")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)

    result = await provision(hass, result, name="Flat", role="auto")

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert result["errors"] == {"base": "provision_failed"}
    assert result["description_placeholders"]["step"] == "public_key"
    plug.stall_at = None
    result = await provision(hass, result, name="Renamed", role="auto")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].title == "Renamed"  # the name of the last submit
    assert result["result"].data["name"] == "Renamed"
    # the same network, not a second one: same keys, one address 0x0001 reserved for it
    assert result["result"].data["net_key"] == NET_KEY.hex() == plug.provisioned.net_key.hex()
    assert result["result"].data["app_key"] == APP_KEY.hex()
    network_id = result["result"].unique_id
    assert await AddressBook(hass).async_used(network_id) == [(0x0001, 1)]
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_device_that_stopped_advertising_is_gone(
    hass: HomeAssistant, radio: SimRadio
) -> None:
    result = await discover(hass, SimUnprovisioned(PLUG_MAC, name="Plug"))

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "device_gone")


async def test_a_discovery_for_one_of_our_nodes_aborts(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    """HA merges adverts, so a node we provisioned still carries its old 0x1827 data."""
    bulb = SimUnprovisioned(BULB_MAC, name="Mesh CW")
    radio.advertise_unprovisioned(bulb)

    result = await discover(hass, bulb)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")


async def test_an_ignored_device_is_not_offered_again(hass: HomeAssistant, radio: SimRadio) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    MockConfigEntry(domain=DOMAIN, unique_id=plug.uuid.hex(), source=SOURCE_IGNORE).add_to_hass(
        hass
    )

    result = await discover(hass, plug)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")


async def test_a_half_provisioned_device_aborts_with_reset_advice(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="complete")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)

    result = await provision(hass, result, network=bulb_entry.entry_id, role="auto")

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "half_provisioned")
    assert result["description_placeholders"] == {"name": "Plug", "step": "complete"}
    assert len(bulb_entry.subentries) == 1


async def test_a_device_being_added_through_add_device_is_refused_in_discovery(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)
    assert provision_module.claim_device(hass, PLUG_MAC)  # Add device got there first

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "role": "auto"}
    )

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert result["errors"] == {"base": "already_provisioning"}
    provision_module.release_device(hass, PLUG_MAC)


async def test_a_busy_device_with_nothing_to_retry_aborts_with_the_reason(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="public_key")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "role": "auto"}
    )
    await hass.async_block_till_done()
    radio.forget(PLUG_MAC)  # it left while we waited

    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "provision_failed")
    assert result["description_placeholders"]["step"] == "public_key"


async def test_an_unexpected_error_returns_to_the_device_form_as_unknown(
    hass: HomeAssistant, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("surprise")

    monkeypatch.setattr(config_flow, "async_provision_device", boom)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "role": "auto"}
    )
    await hass.async_block_till_done()
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert result["errors"] == {"base": "unknown"}
    assert hass.data[DOMAIN]["provisioning"] == set()


async def test_discovering_a_device_again_does_not_start_a_second_flow(
    hass: HomeAssistant, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    first = await discover(hass, plug)

    second = await discover(hass, plug)

    assert first["type"] is FlowResultType.FORM
    assert (second["type"], second["reason"]) == (FlowResultType.ABORT, "already_in_progress")
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_a_new_network_that_cannot_reserve_our_address_aborts(
    hass: HomeAssistant, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(AddressBook, "async_add", failing)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "role": "auto"}
    )

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "cannot_save")
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_retry_with_another_role_stores_the_new_role(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="public_key")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)
    result = await provision(hass, result, name="Flat", role="auto")
    assert result["errors"] == {"base": "provision_failed"}
    plug.stall_at = None

    result = await provision(hass, result, name="Flat", role="light")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = result["result"].subentries.values()
    assert subentry.data["role"] == "light"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_save_failure_while_adding_shows_a_translated_error(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(AddressBook, "async_reserve_node", failing)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)

    result = await provision(hass, result, name="Flat", role="auto")

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert result["errors"] == {"base": "cannot_save"}


async def test_the_chosen_network_deleted_while_the_form_was_open_aborts(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    only = make_entry(network_id="0011223344556677")
    only.add_to_hass(hass)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)
    await hass.config_entries.async_remove(only.entry_id)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"network": only.entry_id, "role": "auto"}
    )

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "network_gone")


async def test_a_name_submitted_after_a_network_appeared_shows_the_form_again(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)  # no network yet: the form asks for a name
    make_entry(network_id="0011223344556677").add_to_hass(hass)  # another dialog made one

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "role": "auto"}
    )

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    assert [str(key) for key in result["data_schema"].schema] == ["network", "role"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1  # nothing was made


async def test_a_retry_after_the_target_network_was_deleted_does_not_use_its_keys(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    target = make_entry(network_id="0011223344556677")
    target.add_to_hass(hass)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="public_key")
    radio.advertise_unprovisioned(plug)
    result = await discover(hass, plug)
    result = await provision(hass, result, network=target.entry_id, role="auto")
    assert result["errors"] == {"base": "provision_failed"}
    await hass.config_entries.async_remove(target.entry_id)
    plug.stall_at = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"])  # form, redrawn
    assert [str(key) for key in result["data_schema"].schema] == ["name", "role"]

    result = await provision(hass, result, name="Fresh", role="auto")

    assert result["type"] is FlowResultType.CREATE_ENTRY  # a new network, not the deleted one
    assert result["result"].title == "Fresh"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_the_network_deleted_while_the_device_was_being_added_aborts(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device took the network's keys: it must be reset, and nothing is added anywhere."""
    target = make_entry(network_id="0011223344556677")
    target.add_to_hass(hass)
    plug = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug)
    real = config_flow.async_provision_into_entry

    async def removed_meanwhile(hass: HomeAssistant, entry: Any, device: Any) -> Any:
        await hass.config_entries.async_remove(entry.entry_id)
        return await real(hass, entry, device)

    monkeypatch.setattr(config_flow, "async_provision_into_entry", removed_meanwhile)
    result = await discover(hass, plug)

    result = await provision(hass, result, network=target.entry_id, role="auto")

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "network_gone_after_add")
    assert result["description_placeholders"] == {"name": "Plug"}
    assert plug.provisioned is not None  # it really holds the deleted network's keys
    assert hass.config_entries.async_entries(DOMAIN) == []
