"""Manage device (the node's reconfigure): change type, run setup again, reset and remove."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aoi_sig_mesh.const import SUBENTRY_NODE
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import SimRadio

from .conftest import make_entry, node_data, setup_entry, until

HEALTH_SERVER = 0x0002


async def open_step(hass: HomeAssistant, entry: Any, step: str) -> dict[str, Any]:
    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_NODE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["role", "setup_again", "reset_remove"]
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


@pytest.fixture
async def plug_entry(hass: HomeAssistant, sim: SimMesh, radio: SimRadio) -> Any:
    plug = sim.add_node("onoff", unicast=0x00C0, health=True)
    radio.advertise()
    entry = make_entry(("Kettle", node_data(plug)))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected)
    yield entry
    await hass.async_block_till_done()  # the reload a removal started
    await hass.config_entries.async_unload(entry.entry_id)


def plug_of(sim: SimMesh) -> SimNode:
    return sim.nodes[0x00C0]


async def test_setup_again_binds_health_and_keeps_the_chosen_role(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    """A node set up before Health was bound (or by another app) gets it from setup again."""
    lamp = sim.add_node("ctl", unicast=0x00B0)
    lamp.bindings.discard((0x00B0, HEALTH_SERVER))
    radio.advertise()
    entry = make_entry(("Desk lamp", node_data(lamp, role="switch")))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected)
    subentry_id = next(iter(entry.subentries))

    result = await open_step(hass, entry, "setup_again")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "setup_again")
    assert result["description_placeholders"] == {"name": "Desk lamp"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "setup_again_started")
    await until(lambda: (0x00B0, HEALTH_SERVER) in lamp.bindings)
    await until(lambda: entry.subentries[subentry_id].data["configured"])
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator  # no reload: the connection stayed up
    assert entry.subentries[subentry_id].data["role"] == "switch"
    assert hass.states.get("switch.desk_lamp") is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_again_on_a_network_that_is_not_running_waits_for_it(
    hass: HomeAssistant, sim: SimMesh
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0)
    entry = make_entry(("Kettle", node_data(plug)))
    entry.add_to_hass(hass)  # not set up

    result = await open_step(hass, entry, "setup_again")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})

    assert result["reason"] == "setup_again_started"
    assert next(iter(entry.subentries.values())).data["configured"] is False


async def test_reset_and_remove_resets_the_node_then_deletes_it(
    hass: HomeAssistant, sim: SimMesh, plug_entry: Any
) -> None:
    result = await open_step(hass, plug_entry, "reset_remove")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "reset_remove")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": False}
    )

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "device_reset")
    assert result["description_placeholders"] == {"name": "Kettle"}
    assert plug_of(sim).reset
    assert plug_entry.subentries == {}
    await hass.async_block_till_done()
    assert hass.states.get("switch.kettle") is None


async def test_an_unconfirmed_reset_keeps_the_device_unless_asked_to_remove_it(
    hass: HomeAssistant, sim: SimMesh, plug_entry: Any
) -> None:
    sim.silent = True
    result = await open_step(hass, plug_entry, "reset_remove")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": False}
    )

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "reset_remove")
    assert result["errors"] == {"base": "reset_failed"}
    assert result["description_placeholders"] == {"name": "Kettle"}
    assert len(plug_entry.subentries) == 1

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": True}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "device_removed")
    assert plug_entry.subentries == {}
    assert plug_of(sim).reset  # the sim reset; only its status was lost


@pytest.mark.parametrize(("remove_anyway", "removed"), [(False, False), (True, True)])
async def test_a_network_that_is_not_running_cannot_reset(
    hass: HomeAssistant, sim: SimMesh, remove_anyway: bool, removed: bool
) -> None:
    plug = sim.add_node("onoff", unicast=0x00C0)
    entry = make_entry(("Kettle", node_data(plug)))
    entry.add_to_hass(hass)

    result = await open_step(hass, entry, "reset_remove")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": remove_anyway}
    )

    assert (entry.subentries == {}) is removed
    assert result.get("reason", result.get("errors")) == (
        "device_removed" if removed else {"base": "reset_failed"}
    )
    assert not plug.reset
