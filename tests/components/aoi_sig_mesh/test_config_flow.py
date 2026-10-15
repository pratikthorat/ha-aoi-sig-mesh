"""Import from tuya_ble_mesh: new network, joining a network, and Bluetooth discovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    SOURCE_IGNORE,
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aoi_sig_mesh.const import DOMAIN, PROXY_SERVICE_UUID, TUYA_DOMAIN
from custom_components.aoi_sig_mesh.store import AddressBook, _StrictStore
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimNode

from .conftest import NETWORK_ID, make_entry, node_data

DEV_KEY = bytes(range(16))
TUYA_DATA: dict[str, Any] = {
    "device_type": "sig_plug",
    "mac_address": "dc:23:50:00:00:19",
    "unicast_target": "00B0",
    "unicast_our": "0001",
    "iv_index": 3,
    "net_key": NET_KEY.hex(),
    "app_key": APP_KEY.hex(),
    "dev_key": DEV_KEY.hex(),
    "sig_models": {
        "cid": 0x07D0,
        "pid": 0x0280,
        "vid": 0x3737,
        "elements": 2,
        "onoff": 0xB0,
        "lightness": 0xB0,
        "ctl": 0xB0,
        "ctl_temp": 0xB1,
        "temp_range": [2700, 6500],
    },
}


@pytest.fixture(autouse=True)
async def no_setup(hass: HomeAssistant) -> AsyncIterator[None]:
    """Flow tests stop at the entry; setup (and so unload) has its own tests.

    The entries are unloaded here, while the stub is in place: the real unload would
    shut down a coordinator the stubbed setup never made.
    """
    with (
        patch("custom_components.aoi_sig_mesh.async_setup_entry", return_value=True),
        patch("custom_components.aoi_sig_mesh.async_unload_entry", return_value=True),
    ):
        yield
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)


def tuya_entry(hass: HomeAssistant, title: str = "Mesh CW", **changes: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=TUYA_DOMAIN, title=title, data={**TUYA_DATA, **changes})
    entry.add_to_hass(hass)
    return entry


async def run_import(
    hass: HomeAssistant, source: MockConfigEntry, *, disable_old: bool = True
) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tuya_import"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_entry": source.entry_id, "name": "Home mesh"}
    )
    if result["type"] is not FlowResultType.FORM or result["step_id"] != "disable_old":
        return result
    assert result["description_placeholders"] == {"old": source.title}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"disable_old": disable_old}
    )
    await hass.async_block_till_done()
    return result


async def test_nothing_to_import_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_import_source"


async def test_import_creates_the_network_and_disables_the_old_entry(
    hass: HomeAssistant,
) -> None:
    source = tuya_entry(hass)

    result = await run_import(hass, source)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == NETWORK_ID
    assert entry.data["address"] == 0x7F00
    assert entry.data["iv_index"] == 3
    assert entry.data["net_key"] == NET_KEY.hex()
    assert entry.options == {"idle_timeout": 0, "publish_setup": True}
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Mesh CW"
    assert subentry.unique_id == "00b0"
    assert subentry.data["role"] == "light"
    assert subentry.data["mac"] == "DC:23:50:00:00:19"
    assert subentry.data["configured"] is False
    assert subentry.data["models"]["ctl_temp"] == 0xB1
    assert source.disabled_by is ConfigEntryDisabler.USER


async def test_import_can_leave_the_old_entry_enabled(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)

    result = await run_import(hass, source, disable_old=False)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert source.disabled_by is None


def fork_entity(hass: HomeAssistant, source: MockConfigEntry, domain: str) -> str:
    """The old entry's entity, holding ``<domain>.mesh_cw``."""
    return (
        er.async_get(hass)
        .async_get_or_create(
            domain,
            TUYA_DOMAIN,
            "dc:23:50:00:00:19",
            config_entry=source,
            suggested_object_id="mesh_cw",
        )
        .id
    )


async def register_our_light(hass: HomeAssistant, entry: Any) -> bool:
    """Stands in for setup: our light, named after the device like the fork's."""
    er.async_get(hass).async_get_or_create(
        "light", DOMAIN, "ours", config_entry=entry, suggested_object_id="mesh_cw"
    )
    return True


def our_light(hass: HomeAssistant) -> str | None:
    return er.async_get(hass).async_get_entity_id("light", DOMAIN, "ours")


async def test_the_old_entitys_id_moves_to_ours(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    fork = fork_entity(hass, source, "light")

    with patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=register_our_light):
        result = await run_import(hass, source)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert our_light(hass) == "light.mesh_cw"
    assert er.async_get(hass).async_get(fork).entity_id == "light.mesh_cw_tuya_ble_mesh"


async def test_a_taken_fallback_id_gets_the_next_free_one(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    fork = fork_entity(hass, source, "light")
    er.async_get(hass).async_get_or_create(
        "light", "other", "x", suggested_object_id="mesh_cw_tuya_ble_mesh"
    )

    with patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=register_our_light):
        await run_import(hass, source)

    assert our_light(hass) == "light.mesh_cw"
    assert er.async_get(hass).async_get(fork).entity_id == "light.mesh_cw_tuya_ble_mesh_2"


async def test_entities_of_different_domains_keep_their_ids(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    fork = fork_entity(hass, source, "switch")

    with patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=register_our_light):
        await run_import(hass, source)

    assert our_light(hass) == "light.mesh_cw"
    assert er.async_get(hass).async_get(fork).entity_id == "switch.mesh_cw"


async def test_a_kept_old_entry_keeps_its_entity_id(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    fork = fork_entity(hass, source, "light")

    with patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=register_our_light):
        await run_import(hass, source, disable_old=False)

    assert our_light(hass) == "light.mesh_cw_2"
    assert er.async_get(hass).async_get(fork).entity_id == "light.mesh_cw"


async def test_a_failed_rename_does_not_fail_the_import(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    fork = fork_entity(hass, source, "light")
    registry = er.async_get(hass)
    real_update = registry.async_update_entity

    def refuse_ours(entity_id: str, **changes: Any) -> Any:
        if entity_id == "light.mesh_cw_2":
            raise ValueError("Entity with this ID is already registered")
        return real_update(entity_id, **changes)

    with (
        patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=register_our_light),
        patch.object(registry, "async_update_entity", side_effect=refuse_ours),
    ):
        result = await run_import(hass, source)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert source.disabled_by is ConfigEntryDisabler.USER
    # Ours could not move, so the old entity got its id back.
    assert our_light(hass) == "light.mesh_cw_2"
    assert registry.async_get(fork).entity_id == "light.mesh_cw"


async def test_reimport_after_delete_never_reuses_our_address(hass: HomeAssistant) -> None:
    """The bulb still answers 0x7F00 and its replay cache holds our old seq: take a new one."""
    source = tuya_entry(hass)
    first = (await run_import(hass, source))["result"]
    await hass.config_entries.async_remove(first.entry_id)
    await hass.config_entries.async_set_disabled_by(source.entry_id, None)

    second = (await run_import(hass, source))["result"]

    assert first.data["address"] == 0x7F00
    assert second.data["address"] == 0x7F01


async def test_a_second_device_joins_the_network_it_belongs_to(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    existing = make_entry(("Desk lamp", node_data(bulb)))
    existing.add_to_hass(hass)
    source = tuya_entry(
        hass,
        "Hall bulb",
        unicast_target="00C0",
        mac_address="dc:23:50:00:00:03",
        unicast_our="0005",
        sig_models={"elements": 1, "onoff": 0xC0},
    )

    result = await run_import(hass, source)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "node_added"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    added = next(sub for sub in existing.subentries.values() if sub.title == "Hall bulb")
    assert added.unique_id == "00c0"
    assert added.data["role"] == "switch"
    assert existing.data["address"] == 0x0001  # the network keeps its own address
    assert source.disabled_by is ConfigEntryDisabler.USER


async def test_a_device_already_in_the_network_aborts(hass: HomeAssistant, bulb: SimNode) -> None:
    make_entry(("Desk lamp", node_data(bulb))).add_to_hass(hass)
    source = tuya_entry(hass, unicast_our="0005")

    result = await run_import(hass, source)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert source.disabled_by is None


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"net_key": "zz"}, "bad_keys"),
        ({"dev_key": None}, "bad_keys"),
        ({"unicast_target": "C000"}, "bad_address"),
        ({"iv_index": -1}, "bad_iv_index"),
        ({"mac_address": "not-a-mac"}, "bad_mac"),
        ({"mac_address": None}, "bad_mac"),
    ],
)
async def test_a_broken_entry_shows_why(
    hass: HomeAssistant, changes: dict[str, Any], error: str
) -> None:
    source = tuya_entry(hass, **changes)

    result = await run_import(hass, source)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tuya_import"
    assert result["errors"] == {"base": error}


async def open_disable_old(hass: HomeAssistant, source: MockConfigEntry) -> str:
    """Walk to the disable_old form and return the flow id."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tuya_import"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_entry": source.entry_id, "name": "Home mesh"}
    )
    assert result["step_id"] == "disable_old"
    return result["flow_id"]


async def test_an_ignored_discovery_is_replaced_by_the_import(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=NETWORK_ID, data={}).add_to_hass(
        hass
    )
    source = tuya_entry(hass)

    result = await run_import(hass, source)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.data["address"] == 0x7F00
    assert source.disabled_by is ConfigEntryDisabler.USER


async def test_the_old_entry_is_still_enabled_while_the_new_one_is_set_up(
    hass: HomeAssistant,
) -> None:
    source = tuya_entry(hass)
    seen: list[ConfigEntryDisabler | None] = []

    async def setup(hass: HomeAssistant, entry: Any) -> bool:
        seen.append(source.disabled_by)
        return True

    with patch("custom_components.aoi_sig_mesh.async_setup_entry", side_effect=setup):
        await run_import(hass, source)

    assert seen == [None]
    assert source.disabled_by is ConfigEntryDisabler.USER


async def test_a_failed_creation_leaves_the_old_entry_enabled(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)
    flow_id = await open_disable_old(hass, source)

    with (
        patch.object(hass.config_entries, "async_add", side_effect=HomeAssistantError),
        pytest.raises(HomeAssistantError),
    ):
        await hass.config_entries.flow.async_configure(flow_id, {"disable_old": True})
    await hass.async_block_till_done()

    assert source.disabled_by is None
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_a_join_that_aborts_at_submit_leaves_the_old_entry_enabled(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    existing = make_entry()
    existing.add_to_hass(hass)
    source = tuya_entry(hass, unicast_our="0005")
    flow_id = await open_disable_old(hass, source)
    # The same device arrives by another route while the form is open.
    hass.config_entries.async_add_subentry(
        existing,
        ConfigSubentry(
            data=MappingProxyType(node_data(bulb, mac=None) | {"unicast": 0xB0}),
            subentry_type="node",
            title="Other",
            unique_id="00b0",
        ),
    )

    result = await hass.config_entries.flow.async_configure(flow_id, {"disable_old": True})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert source.disabled_by is None


async def test_unticking_disable_leaves_a_joined_source_enabled(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    make_entry(("Desk lamp", node_data(bulb))).add_to_hass(hass)
    source = tuya_entry(
        hass,
        unicast_our="0005",
        unicast_target="00C0",
        sig_models={"elements": 1, "onoff": 0xC0},
    )

    result = await run_import(hass, source, disable_old=False)

    assert result["reason"] == "node_added"
    assert source.disabled_by is None


async def test_the_network_vanishing_at_submit_aborts(hass: HomeAssistant) -> None:
    existing = make_entry()
    existing.add_to_hass(hass)
    source = tuya_entry(hass, unicast_our="0005")
    flow_id = await open_disable_old(hass, source)
    await hass.config_entries.async_remove(existing.entry_id)

    result = await hass.config_entries.flow.async_configure(flow_id, {"disable_old": True})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "network_gone"
    assert source.disabled_by is None


async def test_a_node_overlapping_the_device_added_while_the_form_is_open_aborts(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    """The submit-time recheck: a different node now claims 0x00B1, the device's 2nd element."""
    existing = make_entry()
    existing.add_to_hass(hass)
    source = tuya_entry(hass, unicast_our="0005")
    flow_id = await open_disable_old(hass, source)
    other = {**node_data(bulb, mac=None), "unicast": 0xB1, "element_count": 1}
    hass.config_entries.async_add_subentry(
        existing,
        ConfigSubentry(
            data=MappingProxyType(other), subentry_type="node", title="Other", unique_id="00b1"
        ),
    )

    result = await hass.config_entries.flow.async_configure(flow_id, {"disable_old": True})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not any(sub.unique_id == "00b0" for sub in existing.subentries.values())
    assert source.disabled_by is None


async def test_a_join_refused_by_home_assistant_leaves_the_old_entry_enabled(
    hass: HomeAssistant,
) -> None:
    make_entry().add_to_hass(hass)
    source = tuya_entry(hass, unicast_our="0005")

    with patch.object(
        hass.config_entries, "async_add_subentry", side_effect=AbortFlow("already_configured")
    ):
        result = await run_import(hass, source)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert source.disabled_by is None


async def test_an_ignored_discovery_removed_while_saving_does_not_fail_the_import(
    hass: HomeAssistant,
) -> None:
    ignored = MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=NETWORK_ID, data={})
    ignored.add_to_hass(hass)
    source = tuya_entry(hass)
    real_add = AddressBook.async_add

    async def add_and_unignore(self: AddressBook, network_id: str, address: int) -> None:
        await real_add(self, network_id, address)
        await hass.config_entries.async_remove(ignored.entry_id)  # the user un-ignored it

    with patch.object(AddressBook, "async_add", add_and_unignore):
        result = await run_import(hass, source)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.data["address"] == 0x7F00


async def test_a_damaged_subentry_does_not_break_a_join(hass: HomeAssistant, bulb: SimNode) -> None:
    existing = make_entry(("Desk lamp", node_data(bulb)))
    existing.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        existing,
        ConfigSubentry(
            data=MappingProxyType({"junk": True}),
            subentry_type="node",
            title="Broken",
            unique_id="broken",
        ),
    )
    source = tuya_entry(
        hass,
        unicast_our="0005",
        unicast_target="00C0",
        sig_models={"elements": 1, "onoff": 0xC0},
    )

    result = await run_import(hass, source)

    assert result["reason"] == "node_added"
    assert any(sub.unique_id == "00c0" for sub in existing.subentries.values())


async def test_a_device_overlapping_a_claimed_range_aborts(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    make_entry(("Desk lamp", node_data(bulb))).add_to_hass(hass)
    source = tuya_entry(
        hass,
        unicast_our="0005",
        unicast_target="00AF",
        sig_models={"elements": 2, "onoff": 0xAF},
    )

    result = await run_import(hass, source)

    assert result["reason"] == "already_configured"
    assert source.disabled_by is None


async def test_a_device_containing_the_networks_own_address_aborts(hass: HomeAssistant) -> None:
    make_entry(address=0x0003).add_to_hass(hass)
    source = tuya_entry(
        hass,
        unicast_our="0005",
        unicast_target="0002",
        sig_models={"elements": 3, "onoff": 0x0002},
    )

    result = await run_import(hass, source)

    assert result["reason"] == "already_configured"


async def test_a_fork_that_sent_from_the_networks_own_address_is_refused(
    hass: HomeAssistant, bulb: SimNode
) -> None:
    make_entry(("Desk lamp", node_data(bulb)), address=0x0005).add_to_hass(hass)
    source = tuya_entry(
        hass,
        unicast_our="0005",
        unicast_target="00C0",
        sig_models={"elements": 1, "onoff": 0xC0},
    )

    result = await run_import(hass, source)

    assert result["reason"] == "address_conflict"
    assert source.disabled_by is None


async def test_an_unsaved_address_book_creates_nothing(hass: HomeAssistant) -> None:
    source = tuya_entry(hass)

    with patch.object(_StrictStore, "async_save_strict", AsyncMock(side_effect=OSError("disk"))):
        result = await run_import(hass, source)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_save"
    assert not hass.config_entries.async_entries(DOMAIN)
    assert source.disabled_by is None


def proxy_advert(data: bytes) -> BluetoothServiceInfo:
    return BluetoothServiceInfo(
        name="Mesh CW",
        address="DC:23:50:00:00:19",
        rssi=-60,
        manufacturer_data={},
        service_data={PROXY_SERVICE_UUID: data},
        service_uuids=[PROXY_SERVICE_UUID],
        source="local",
    )


async def discover(hass: HomeAssistant, data: bytes) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=proxy_advert(data)
    )


async def test_discovery_of_an_importable_network_offers_the_import(
    hass: HomeAssistant,
) -> None:
    tuya_entry(hass)

    result = await discover(hass, b"\x00" + bytes.fromhex(NETWORK_ID))

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["tuya_import"]


async def test_discovery_of_a_network_we_have_aborts(hass: HomeAssistant, bulb: SimNode) -> None:
    make_entry(("Desk lamp", node_data(bulb))).add_to_hass(hass)
    tuya_entry(hass)

    result = await discover(hass, b"\x00" + bytes.fromhex(NETWORK_ID))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    "data",
    [
        b"\x00" + bytes.fromhex(NETWORK_ID),  # no tuya entry to import from
        b"\x01" + bytes(16),  # Node Identity: cannot tell the network
        b"\x07",  # garbage
    ],
)
async def test_discovery_we_cannot_act_on_aborts(hass: HomeAssistant, data: bytes) -> None:
    result = await discover(hass, data)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"
