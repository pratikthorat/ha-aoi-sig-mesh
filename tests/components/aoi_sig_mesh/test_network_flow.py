"""Create a network, or import one from an nRF Mesh export."""

from __future__ import annotations

import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_IGNORE, SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aoi_sig_mesh.const import DOMAIN, TUYA_DOMAIN
from custom_components.aoi_sig_mesh.mesh import importers
from custom_components.aoi_sig_mesh.mesh.pdu import NetworkKeys
from custom_components.aoi_sig_mesh.store import AddressBook, _StrictStore
from tests.mesh.test_nrf_import import BULB_KEY, PLUG_KEY, export
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimMesh
from tests.sim.sim_radio import SimRadio

from .conftest import NETWORK_ID, make_entry, until


async def open_menu(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return result


async def create(hass: HomeAssistant, name: str = "Flat") -> dict[str, Any]:
    result = await open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "create"}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "create")
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"name": name})


async def nrf_import(hass: HomeAssistant, text: str) -> dict[str, Any]:
    result = await open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nrf_import"}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "nrf_import")
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"nrf_json": text})


@pytest.fixture
def no_setup():
    with patch("custom_components.aoi_sig_mesh.async_setup_entry", return_value=True):
        yield


async def test_the_menu_offers_create_and_nrf_import(hass: HomeAssistant) -> None:
    assert (await open_menu(hass))["menu_options"] == ["create", "nrf_import"]
    MockConfigEntry(domain=TUYA_DOMAIN, data={"device_type": "sig_plug"}).add_to_hass(hass)
    assert (await open_menu(hass))["menu_options"] == ["create", "nrf_import", "tuya_import"]


@pytest.mark.usefixtures("no_setup")
async def test_create_makes_fresh_keys_and_reserves_our_address(hass: HomeAssistant) -> None:
    first = (await create(hass, "Flat"))["result"]
    second = (await create(hass, "Studio"))["result"]

    assert first.title == "Flat"
    assert first.data["address"] == 0x0001
    assert (first.data["range_low"], first.data["range_high"]) == (0x0002, 0x7EFF)
    assert (first.data["iv_index"], first.data["net_key_index"]) == (0, 0)
    assert first.options == {"idle_timeout": 0, "publish_setup": True}
    assert first.subentries == {}
    keys = {first.data["net_key"], first.data["app_key"], second.data["net_key"]}
    assert len(keys) == 3 and all(len(bytes.fromhex(key)) == 16 for key in keys)
    network_id = NetworkKeys.from_net_key(bytes.fromhex(first.data["net_key"])).network_id
    assert first.unique_id == network_id.hex()
    assert await AddressBook(hass).async_used(first.unique_id) == [(0x0001, 1)]


async def test_create_without_a_saved_address_creates_nothing(hass: HomeAssistant) -> None:
    with patch.object(_StrictStore, "async_save_strict", AsyncMock(side_effect=OSError("disk"))):
        result = await create(hass)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "cannot_save")
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.usefixtures("no_setup")
async def test_nrf_import_creates_the_network_and_its_nodes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    data = export()
    data["nodes"][2]["excluded"] = True
    caplog.set_level(logging.DEBUG)

    result = await nrf_import(hass, json.dumps(data))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert (entry.title, entry.unique_id) == ("Home", NETWORK_ID)
    assert entry.data["net_key"] == NET_KEY.hex()
    assert (entry.data["app_key"], entry.data["app_key_index"]) == (APP_KEY.hex(), 1)
    assert entry.data["address"] == 0x7F00
    assert (entry.data["range_low"], entry.data["range_high"]) == (0x199B, 0x7EFF)
    (lamp,) = entry.subentries.values()
    assert (lamp.title, lamp.unique_id) == ("Desk lamp", "0002")
    assert lamp.data["role"] == "light"
    assert lamp.data["mac"] is None
    assert lamp.data["configured"] is True
    assert await AddressBook(hass).async_used(NETWORK_ID) == [(0x7F00, 1)]
    # the excluded plug is still on the mesh: Add device must never give out 0x0004
    assert await AddressBook(hass).async_used_nodes(NETWORK_ID) == [(1, 1), (2, 2), (4, 1)]
    assert "1 nodes were not imported" in caplog.text
    for key in (NET_KEY, APP_KEY, BULB_KEY, PLUG_KEY):
        assert key.hex() not in caplog.text.lower()


@pytest.mark.usefixtures("no_setup")
async def test_a_reimport_never_reuses_our_old_address(hass: HomeAssistant) -> None:
    await AddressBook(hass).async_add(NETWORK_ID, 0x7F00)

    result = await nrf_import(hass, json.dumps(export()))

    assert result["result"].data["address"] == 0x7F01


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("{", "nrf_bad_json"),
        (json.dumps({**export(), "netKeys": []}), "nrf_bad_keys"),
        (json.dumps({**export(), "appKeys": []}), "nrf_no_app_key"),
        (json.dumps({**export(), "ivIndex": -1}), "nrf_bad_iv_index"),
        (
            json.dumps(
                {**export(), "netKeys": [{**export()["netKeys"][0], "phase": 1}]},
            ),
            "nrf_key_refresh",
        ),
        (
            json.dumps(
                {**export(), "nodes": [{**export()["nodes"][1], "unicastAddress": "ZZZZ"}]},
            ),
            "nrf_bad_address",
        ),
    ],
)
async def test_a_bad_export_shows_why_and_keeps_the_form(
    hass: HomeAssistant, text: str, error: str
) -> None:
    result = await nrf_import(hass, text)

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "nrf_import")
    assert result["errors"] == {"base": error}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_an_export_with_no_free_address_is_no_address(hass: HomeAssistant) -> None:
    data = export()
    data["provisioners"][0]["allocatedUnicastRange"] = [
        {"lowAddress": "0001", "highAddress": "7FFF"}
    ]

    result = await nrf_import(hass, json.dumps(data))

    assert result["errors"] == {"base": "no_address"}


async def test_importing_a_network_we_have_aborts(hass: HomeAssistant) -> None:
    make_entry().add_to_hass(hass)

    result = await nrf_import(hass, json.dumps(export()))

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")


@pytest.mark.usefixtures("no_setup")
async def test_an_ignored_discovery_is_replaced_by_the_import(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=NETWORK_ID, source=SOURCE_IGNORE).add_to_hass(hass)

    result = await nrf_import(hass, json.dumps(export()))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [e.source for e in hass.config_entries.async_entries(DOMAIN)] == [SOURCE_USER]


async def test_an_imported_network_runs_and_sets_up_the_unbound_plug(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """End to end: the lamp is used as exported; the plug is set up on the first connect."""
    lamp = sim.add_node("ctl", unicast=0x0002)
    plug = sim.add_node("onoff", unicast=0x0004, configured=False)
    data = export()
    data["nodes"][1]["deviceKey"] = lamp.dev_key.hex()
    data["nodes"][2]["deviceKey"] = plug.dev_key.hex()
    radio.advertise()

    result = await nrf_import(hass, json.dumps(data))
    await hass.async_block_till_done()
    coordinator = result["result"].runtime_data
    await until(lambda: coordinator.connected and plug.has_app_key)
    await until(lambda: all(record.configured for record in coordinator.nodes.values()))
    await until(lambda: hass.states.get("switch.kettle_plug") is not None)

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.kettle_plug"}, blocking=True
    )
    await until(lambda: plug.on)
    assert hass.states.get("light.desk_lamp") is not None
    assert lamp.received  # read with the exported bindings, no setup needed
    await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.usefixtures("no_setup")
async def test_create_uses_the_generated_keys_and_never_logs_them(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with patch(
        "custom_components.aoi_sig_mesh.config_flow.secrets.token_bytes",
        side_effect=[NET_KEY, APP_KEY],
    ):
        result = await create(hass)

    entry = result["result"]
    assert entry.unique_id == NETWORK_ID
    assert (entry.data["net_key"], entry.data["app_key"]) == (NET_KEY.hex(), APP_KEY.hex())
    for key in (NET_KEY, APP_KEY):
        assert key.hex() not in caplog.text.lower()


async def test_nrf_import_without_a_saved_address_aborts_and_keeps_the_ignored_entry(
    hass: HomeAssistant,
) -> None:
    ignored = MockConfigEntry(domain=DOMAIN, unique_id=NETWORK_ID, source=SOURCE_IGNORE)
    ignored.add_to_hass(hass)

    with patch.object(_StrictStore, "async_save_strict", AsyncMock(side_effect=OSError("disk"))):
        result = await nrf_import(hass, json.dumps(export()))

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "cannot_save")
    assert [(e.entry_id, e.source) for e in hass.config_entries.async_entries(DOMAIN)] == [
        (ignored.entry_id, SOURCE_IGNORE)
    ]


@pytest.mark.usefixtures("no_setup")
async def test_a_reimport_never_picks_an_address_a_node_already_holds(
    hass: HomeAssistant,
) -> None:
    await AddressBook(hass).async_add_nodes(NETWORK_ID, [(0x7F00, 1)])

    result = await nrf_import(hass, json.dumps(export()))

    assert result["result"].data["address"] == 0x7F01


def test_every_reason_the_importer_can_raise_has_an_error_string() -> None:
    reasons = set(re.findall(r'NrfImportError\(\s*"(\w+)"', inspect.getsource(importers)))
    assert {"bad_json", "bad_keys", "key_refresh", "bad_address", "no_address"} <= reasons
    errors = json.loads(
        (Path(__file__).parents[3] / "custom_components/aoi_sig_mesh/strings.json").read_text()
    )["config"]["error"]
    for reason in reasons:
        key = "no_address" if reason == "no_address" else f"nrf_{reason}"
        assert errors.get(key), f"{key} has no string"
