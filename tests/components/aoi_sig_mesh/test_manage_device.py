"""Manage device (the node's reconfigure): change type, run setup again, reset and remove."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.aoi_sig_mesh.const import (
    CONF_IDLE_TIMEOUT,
    CONF_PUBLISH_SETUP,
    SUBENTRY_NODE,
)
from custom_components.aoi_sig_mesh.coordinator import FAILURES_UNAVAILABLE
from tests.sim.mesh_sim import SimMesh, SimNode
from tests.sim.sim_radio import BULB_MAC, SimRadio

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


@pytest.fixture(autouse=True)
def rediscover() -> Iterator[MagicMock]:
    """bluetooth.async_rediscover_address (HA's bluetooth is not set up in these tests)."""
    with patch(
        "homeassistant.components.bluetooth.async_rediscover_address", autospec=True
    ) as mock:
        yield mock


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
    hass: HomeAssistant, sim: SimMesh, remove_anyway: bool, removed: bool, rediscover: MagicMock
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
        "device_removed" if removed else {"base": "reset_not_running"}
    )
    assert not plug.reset
    rediscover.assert_not_called()


async def test_a_confirmed_reset_lets_bluetooth_discover_the_device_again(
    hass: HomeAssistant, plug_entry: Any, rediscover: MagicMock
) -> None:
    """HA's matcher already holds 0x1827 for this address, so without a rediscover the reset
    device would never show up as discovered again."""
    removed_first: list[bool] = []
    rediscover.side_effect = lambda _hass, _address: removed_first.append(
        plug_entry.subentries == {}
    )
    result = await open_step(hass, plug_entry, "reset_remove")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": False}
    )

    assert result["reason"] == "device_reset"
    rediscover.assert_called_once_with(hass, BULB_MAC)
    assert removed_first == [True]  # else the new discovery would see it as one of our nodes


async def test_an_unconfirmed_reset_does_not_rediscover_the_device(
    hass: HomeAssistant, sim: SimMesh, plug_entry: Any, rediscover: MagicMock
) -> None:
    sim.silent = True
    result = await open_step(hass, plug_entry, "reset_remove")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"remove_anyway": True}
    )

    assert result["reason"] == "device_removed"
    rediscover.assert_not_called()


async def test_a_confirmed_reset_forgets_the_nodes_replay_entry(
    hass: HomeAssistant, plug_entry: Any
) -> None:
    coordinator = plug_entry.runtime_data
    result = await open_step(hass, plug_entry, "reset_remove")

    with patch.object(
        coordinator, "async_forget_node", wraps=coordinator.async_forget_node
    ) as forget:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"remove_anyway": False}
        )

    assert result["reason"] == "device_reset"
    forget.assert_called_once_with(0x00C0, 1)


async def test_deleting_anyway_after_a_silent_reset_does_not_forget_the_replay_entry(
    hass: HomeAssistant, sim: SimMesh, plug_entry: Any
) -> None:
    sim.silent = True
    coordinator = plug_entry.runtime_data
    result = await open_step(hass, plug_entry, "reset_remove")

    with patch.object(
        coordinator, "async_forget_node", wraps=coordinator.async_forget_node
    ) as forget:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"remove_anyway": True}
        )

    assert result["reason"] == "device_removed"
    forget.assert_not_called()


async def test_silent_resets_never_make_the_node_unavailable(
    hass: HomeAssistant, sim: SimMesh, plug_entry: Any
) -> None:
    coordinator = plug_entry.runtime_data
    subentry_id = next(iter(plug_entry.subentries))
    record = coordinator.nodes[subentry_id]
    sim.silent = True

    for _ in range(FAILURES_UNAVAILABLE + 1):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_reset_node(subentry_id)

    assert record.failures == 0
    assert record.available
    assert hass.states.get("switch.kettle").state != "unavailable"


async def test_setup_again_on_an_idle_link_connects_at_once(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    lamp = sim.add_node("ctl", unicast=0x00B0)
    lamp.bindings.discard((0x00B0, HEALTH_SERVER))
    radio.advertise()
    options = {CONF_IDLE_TIMEOUT: 0.005, CONF_PUBLISH_SETUP: True}  # 0.3 s
    entry = make_entry(("Desk lamp", node_data(lamp)), options=options)
    coordinator = await setup_entry(hass, entry)
    try:
        await until(lambda: coordinator.manager.idle and coordinator.manager.link is None)
        subentry_id = next(iter(entry.subentries))

        coordinator.async_setup_again(subentry_id)

        await until(lambda: (0x00B0, HEALTH_SERVER) in lamp.bindings)
        await until(lambda: coordinator.nodes[subentry_id].configured)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
