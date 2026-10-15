"""Options, the node subentry flow, and when a change reloads the network."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.aoi_sig_mesh.const import DOMAIN, SUBENTRY_NODE
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator
from tests.sim.mesh_sim import SimMesh
from tests.sim.sim_radio import SimRadio

from .conftest import NETWORK_ID, PLUG_MAC, make_entry, node_data, setup_entry, until


async def _set_role(hass: HomeAssistant, entry: Any, role: str) -> None:
    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_NODE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"role": role})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await until(lambda: entry.runtime_data.connected)


async def test_options_reload_with_the_new_idle_timeout(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    result = await hass.config_entries.options.async_init(bulb_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"idle_timeout": 30, "publish_setup": False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert bulb_entry.options == {"idle_timeout": 30, "publish_setup": False}
    assert bulb_entry.runtime_data.manager._idle_timeout == 1800


async def test_changing_the_role_swaps_the_light_for_a_switch(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    await _set_role(hass, bulb_entry, "switch")

    assert hass.states.get("switch.desk_lamp") is not None
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("light", DOMAIN, f"{NETWORK_ID}_00b0_light") is None


async def test_changing_the_role_back_leaves_one_light_and_no_switch(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    await _set_role(hass, bulb_entry, "switch")
    await _set_role(hass, bulb_entry, "light")

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, bulb_entry.entry_id)
    assert [e.unique_id for e in entities if e.domain == "light"] == [f"{NETWORK_ID}_00b0_light"]
    assert [e for e in entities if e.domain == "switch"] == []
    # the identify button follows the node's capabilities, not its role: it survives both swaps
    assert [e.unique_id for e in entities if e.domain == "button" and "00b0" in e.unique_id] == [
        f"{NETWORK_ID}_00b0_identify"
    ]
    assert hass.states.get("light.desk_lamp") is not None
    assert hass.states.get("switch.desk_lamp") is None


async def test_adding_devices_from_the_subentry_menu_points_to_the_import(
    hass: HomeAssistant, bulb_entry: Any
) -> None:
    result = await hass.config_entries.subentries.async_init(
        (bulb_entry.entry_id, SUBENTRY_NODE), context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "add_by_import"


async def test_adding_and_removing_a_node_reloads_the_network(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    first: MeshCoordinator = bulb_entry.runtime_data
    plug = sim.add_node("onoff", unicast=0x00C0)
    subentry = ConfigSubentry(
        data=MappingProxyType(node_data(plug, mac=PLUG_MAC)),
        subentry_type=SUBENTRY_NODE,
        title="Kitchen plug",
        unique_id="00c0",
    )

    hass.config_entries.async_add_subentry(bulb_entry, subentry)
    await hass.async_block_till_done()

    second: MeshCoordinator = bulb_entry.runtime_data
    assert second is not first
    assert subentry.subentry_id in second.nodes
    await until(lambda: hass.states.get("switch.kitchen_plug").state == "off")

    hass.config_entries.async_remove_subentry(bulb_entry, subentry.subentry_id)
    await hass.async_block_till_done()

    assert subentry.subentry_id not in bulb_entry.runtime_data.nodes
    assert hass.states.get("switch.kitchen_plug") is None
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), bulb_entry.entry_id)
    assert {device.name for device in devices} == {"Home mesh", "Desk lamp"}


async def test_setting_a_node_up_does_not_reload_when_nothing_changed(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """First connect binds the AppKey; the stored capabilities match, so no reload."""
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    # Capabilities as Composition will report them, but not yet set up on the mesh.
    entry = make_entry(("Desk lamp", {**node_data(lamp), "configured": False}))
    coordinator = await setup_entry(hass, entry)
    sid = next(iter(entry.subentries))

    await until(lambda: entry.subentries[sid].data["configured"] is True)
    await hass.async_block_till_done()

    assert entry.runtime_data is coordinator
    assert lamp.has_app_key
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setting_a_node_up_reloads_when_its_capabilities_change(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """The imported colour range was a guess; the bulb's own range rebuilds the light."""
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    lamp.kelvin_min, lamp.kelvin_max = 3000, 5700
    radio.advertise()
    entry = make_entry(("Desk lamp", {**node_data(lamp), "configured": False}))
    first = await setup_entry(hass, entry)

    await until(lambda: entry.runtime_data is not first and entry.runtime_data.connected)

    record = next(iter(entry.runtime_data.nodes.values()))
    assert (record.node.kelvin_min, record.node.kelvin_max) == (3000, 5700)
    assert record.configured

    # The reload settled: the saved capabilities now match, so nothing reloads again.
    settled = entry.runtime_data
    for _ in range(5):
        await hass.async_block_till_done()
    assert entry.runtime_data is settled
    assert settled.connected
    assert not settled.needs_reload()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_role_chosen_before_first_connect_survives_setup(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """Setup refines the colour range of a CTL lamp; that must not undo the chosen role."""
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    lamp.kelvin_min, lamp.kelvin_max = 3000, 5700
    entry = make_entry(("Desk lamp", {**node_data(lamp), "configured": False}))
    await setup_entry(hass, entry)  # no advert yet: not connected
    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_NODE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    await hass.config_entries.subentries.async_configure(result["flow_id"], {"role": "switch"})
    await hass.async_block_till_done()

    radio.advertise()
    await until(lambda: entry.subentries[subentry_id].data["configured"] is True)
    await until(lambda: entry.runtime_data.connected)
    await hass.async_block_till_done()

    record = entry.runtime_data.nodes[subentry_id]
    assert (record.node.kelvin_min, record.node.kelvin_max) == (3000, 5700)
    assert record.role == "switch"
    assert entry.subentries[subentry_id].data["role"] == "switch"
    assert hass.states.get("switch.desk_lamp") is not None
    assert hass.states.get("light.desk_lamp") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_derives_the_role_when_composition_reveals_a_light(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """Imported OnOff-only (a switch), but the node is a lamp: the role follows."""
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    data = {**node_data(lamp, configured=False), "role": "switch"}
    entry = make_entry(("Desk lamp", data))
    await setup_entry(hass, entry)
    sid = next(iter(entry.subentries))

    await until(lambda: entry.subentries[sid].data["role"] == "light")
    await until(lambda: entry.runtime_data.connected and hass.states.get("light.desk_lamp"))

    assert entry.runtime_data.nodes[sid].role == "light"
    assert hass.states.get("switch.desk_lamp") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_updates_the_device_info_without_a_reload(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    data = {**node_data(lamp), "configured": False, "cid": None, "pid": None, "vid": None}
    entry = make_entry(("Desk lamp", data))
    coordinator = await setup_entry(hass, entry)
    sid = next(iter(entry.subentries))
    identifier = (DOMAIN, f"{NETWORK_ID}_00b0")
    device = dr.async_get(hass).async_get_device(identifiers={identifier})
    assert device is not None and device.manufacturer is None

    await until(lambda: entry.subentries[sid].data["configured"] is True)
    await hass.async_block_till_done()

    assert entry.runtime_data is coordinator  # nothing reloaded
    device = dr.async_get(hass).async_get_device(identifiers={identifier})
    assert device.manufacturer == "Tuya"
    assert device.model is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_refining_only_the_element_count_does_not_reload(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    data = {**node_data(lamp), "configured": False, "element_count": 1}
    entry = make_entry(("Desk lamp", data))
    coordinator = await setup_entry(hass, entry)
    sid = next(iter(entry.subentries))

    await until(lambda: entry.subentries[sid].data["configured"] is True)
    await hass.async_block_till_done()

    assert entry.subentries[sid].data["element_count"] == 2
    assert entry.runtime_data is coordinator
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_damaged_sibling_does_not_cause_a_reload(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    lamp = sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True, configured=False)
    radio.advertise()
    entry = make_entry(
        ("Desk lamp", {**node_data(lamp), "configured": False}),
        ("Hall plug", {"unicast": 0x00C0, "element_count": 1}),  # no DevKey: damaged
    )
    coordinator = await setup_entry(hass, entry)
    sid = next(iter(entry.subentries))

    await until(lambda: entry.subentries[sid].data["configured"] is True)
    await hass.async_block_till_done()

    assert entry.runtime_data is coordinator
    assert not coordinator.needs_reload()
    await hass.config_entries.async_unload(entry.entry_id)


async def _form_default_role(hass: HomeAssistant, entry: Any, subentry_id: str) -> Any:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_NODE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    (key,) = result["data_schema"].schema
    return key.default()


@pytest.mark.parametrize(
    ("kind", "stored", "expected"),
    [
        ("ctl", "fan", "light"),  # unreadable node: light
        ("ctl", 7, "light"),
        ("onoff", None, "switch"),  # an explicit None is missing: what the node is
        ("ctl", None, "light"),
        ("ctl", "switch", "switch"),  # a valid stored role is kept
    ],
)
async def test_reconfigure_defaults_to_the_stored_role_only_when_it_is_valid(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, kind: str, stored: Any, expected: str
) -> None:
    node = sim.add_node(kind, unicast=0x00C0)
    entry = make_entry(("Hall node", {**node_data(node), "role": stored}))
    await setup_entry(hass, entry)

    assert await _form_default_role(hass, entry, next(iter(entry.subentries))) == expected
    await hass.config_entries.async_unload(entry.entry_id)
