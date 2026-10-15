"""Home Assistant test harness: the integration runs against tests/sim through SimRadio.

The simulator imports the mesh library as top-level ``mesh``; here that name is aliased to
``custom_components.aoi_sig_mesh.mesh`` so both sides share one set of classes.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import custom_components.aoi_sig_mesh.mesh as _mesh  # noqa: E402

sys.modules["mesh"] = _mesh
for _info in pkgutil.iter_modules(_mesh.__path__):
    sys.modules[f"mesh.{_info.name}"] = importlib.import_module(
        f"custom_components.aoi_sig_mesh.mesh.{_info.name}"
    )

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.aoi_sig_mesh import coordinator as coordinator_module  # noqa: E402
from custom_components.aoi_sig_mesh.const import (  # noqa: E402
    CONF_ADDRESS,
    CONF_APP_KEY,
    CONF_APP_KEY_INDEX,
    CONF_CONFIGURED,
    CONF_IDLE_TIMEOUT,
    CONF_IV_INDEX,
    CONF_MAC,
    CONF_NAME,
    CONF_NET_KEY,
    CONF_NET_KEY_INDEX,
    CONF_PUBLISH_SETUP,
    CONF_RANGE_HIGH,
    CONF_RANGE_LOW,
    CONF_ROLE,
    DOMAIN,
    ROLE_LIGHT,
    ROLE_SWITCH,
    SUBENTRY_NODE,
)
from custom_components.aoi_sig_mesh.coordinator import MeshCoordinator  # noqa: E402
from custom_components.aoi_sig_mesh.mesh import connection, control, proxy  # noqa: E402
from custom_components.aoi_sig_mesh.mesh.node import ROLE_ONOFF, MeshNode  # noqa: E402
from custom_components.aoi_sig_mesh.mesh.pdu import NetworkKeys  # noqa: E402
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY, SimMesh, SimNode  # noqa: E402
from tests.sim.sim_radio import BULB_MAC, SimRadio  # noqa: E402

PLUG_MAC = "DC:23:50:00:00:02"
NETWORK_ID = NetworkKeys.from_net_key(NET_KEY).network_id.hex()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None, hass: HomeAssistant) -> None:
    """Load custom_components/, and count bluetooth as set up (it cannot start on macOS).

    Every test here gets this, so bluetooth's real setup never runs: a test that needs it
    (the manager, real scanners) must override this fixture in its own module.
    """
    hass.config.components.add("bluetooth")


@pytest.fixture(autouse=True)
def no_deprecated_calls(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Home Assistant logs a frame report for each deprecated call we make, and the call
    breaks a few versions later: fail the test instead."""
    yield
    reports = [
        record.getMessage()
        for record in caplog.get_records("call")
        if record.name == "homeassistant.helpers.frame" and DOMAIN in record.getMessage()
    ]
    assert not reports, reports


@pytest.fixture(autouse=True)
def fast_mesh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(proxy, "FILTER_TIMEOUT", 0.2)
    monkeypatch.setattr(connection, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(connection, "BACKOFF_MAX", 0.4)
    monkeypatch.setattr(control, "BLINK_INTERVAL", 0)
    monkeypatch.setattr(coordinator_module, "REFRESH_STAGGER", 0)
    monkeypatch.setattr(coordinator_module, "COMMAND_CONNECT_TIMEOUT", 2.0)


@pytest.fixture
def sim() -> SimMesh:
    return SimMesh()


@pytest.fixture
def bulb(sim: SimMesh) -> SimNode:
    """The Mesh CW bulb: Tuya CTL at 0x00B0, CTL Temperature on element 2."""
    return sim.add_node("ctl", unicast=0x00B0, tuya_quirks=True)


@pytest.fixture
def radio(sim: SimMesh, monkeypatch: pytest.MonkeyPatch) -> SimRadio:
    radio = SimRadio(sim)
    monkeypatch.setattr("custom_components.aoi_sig_mesh.HaBluetoothRadio", lambda hass: radio)
    monkeypatch.setattr(
        "custom_components.aoi_sig_mesh.provision.HaBluetoothRadio", lambda hass: radio
    )
    return radio


def node_data(
    sim_node: SimNode,
    *,
    configured: bool = True,
    role: str | None = None,
    mac: str | None = BULB_MAC,
) -> dict[str, Any]:
    """Subentry data for a simulated node, as setup on the mesh (configured) or as imported."""
    node = MeshNode(
        unicast=sim_node.unicast,
        element_count=sim_node.element_count,
        dev_key=sim_node.dev_key,
        models={ROLE_ONOFF: sim_node.unicast},
    )
    if configured:
        node.apply_composition(sim_node.composition())
    if role is None:
        role = ROLE_LIGHT if node.is_light else ROLE_SWITCH
    return {**node.to_dict(), CONF_ROLE: role, CONF_MAC: mac, CONF_CONFIGURED: configured}


def make_entry(
    *nodes: tuple[str, dict[str, Any]],
    address: int = CLIENT,
    options: dict[str, Any] | None = None,
    network_id: str | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home mesh",
        unique_id=network_id or NETWORK_ID,
        data={
            CONF_NAME: "Home mesh",
            CONF_NET_KEY: NET_KEY.hex(),
            CONF_NET_KEY_INDEX: 0,
            CONF_APP_KEY: APP_KEY.hex(),
            CONF_APP_KEY_INDEX: 0,
            CONF_ADDRESS: address,
            CONF_RANGE_LOW: 0x0002,
            CONF_RANGE_HIGH: 0x7EFF,
            CONF_IV_INDEX: 0,
        },
        options=options or {CONF_IDLE_TIMEOUT: 0, CONF_PUBLISH_SETUP: True},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_NODE,
                "title": title,
                "unique_id": f"{data['unicast']:04x}",
                "data": data,
            }
            for title, data in nodes
        ],
    )


async def until(predicate: Callable[[], Any], timeout: float = 3.0) -> None:
    """Wait (real time) until ``predicate()`` is truthy."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MeshCoordinator:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


@pytest.fixture
async def bulb_entry(
    hass: HomeAssistant, sim: SimMesh, bulb: SimNode, radio: SimRadio
) -> AsyncIterator[MockConfigEntry]:
    """A network with the bulb, set up and connected; unloaded at the end."""
    radio.advertise()
    entry = make_entry(("Desk lamp", node_data(bulb)))
    coordinator = await setup_entry(hass, entry)
    await until(lambda: coordinator.connected and bulb.received)
    await until(lambda: coordinator.nodes and next(iter(coordinator.nodes.values())).last_seen)
    yield entry
    await hass.async_block_till_done()  # a reload a subentry change started
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
