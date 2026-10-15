"""End to end: factory-reset devices found, provisioned, set up and used, with the mesh
invariants checked on everything the network sent."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aoi_sig_mesh import config_flow
from custom_components.aoi_sig_mesh import provision as provision_module
from custom_components.aoi_sig_mesh.mesh import provisioner
from custom_components.aoi_sig_mesh.mesh.network import MeshNetwork
from custom_components.aoi_sig_mesh.store import NetworkStore
from tests.sim.mesh_sim import APP_KEY, NET_KEY, TUYA_VENDOR_MODEL, SimMesh
from tests.sim.prov_sim import SimUnprovisioned
from tests.sim.sim_radio import SimRadio

from .conftest import until
from .test_add_device import add_device
from .test_discovery import discover, provision

PLUG_MAC = "DC:23:50:00:00:27"
LAMP_MAC = "DC:23:50:00:00:28"
OUR_ADDRESS = 0x0001

PROVISIONING = contextvars.ContextVar("provisioning", default=False)


@pytest.fixture(autouse=True)
def fast_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CAPABILITIES_TIMEOUT", "PUBLIC_KEY_TIMEOUT", "COMPLETE_TIMEOUT"):
        monkeypatch.setattr(provisioner, name, 0.2)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


@pytest.fixture(autouse=True)
def sim_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new network gets the sim's keys (NetKey, then AppKey), so the sim can join it."""
    keys: Iterator[bytes] = iter([NET_KEY, APP_KEY])
    monkeypatch.setattr(config_flow, "secrets", SimpleNamespace(token_bytes=lambda n: next(keys)))


@pytest.fixture
def seq_calls(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``calls``: every MeshNetwork.next_seq call, True if made inside a provisioning run.
    ``runs``: how many provisioning runs were observed."""
    calls: list[bool] = []
    runs: list[bool] = []
    real_provision, real_next_seq = provision_module.provision_device, MeshNetwork.next_seq

    async def watched_provision(*args: Any, **kwargs: Any) -> Any:
        PROVISIONING.set(True)  # this task (the flow's progress task) only
        runs.append(True)
        return await real_provision(*args, **kwargs)

    def watched_next_seq(self: MeshNetwork, count: int = 1) -> int:
        calls.append(PROVISIONING.get())
        return real_next_seq(self, count)

    monkeypatch.setattr(provision_module, "provision_device", watched_provision)
    monkeypatch.setattr(MeshNetwork, "next_seq", watched_next_seq)
    return SimpleNamespace(calls=calls, runs=runs)


async def test_factory_reset_devices_become_a_working_switch_and_light(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, seq_calls: SimpleNamespace
) -> None:
    # 1. A factory-reset plug is discovered; there is no network yet, so it gets a new one.
    plug_device = SimUnprovisioned(PLUG_MAC, name="Plug")
    radio.advertise_unprovisioned(plug_device)
    result = await discover(hass, plug_device)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "device")
    result = await provision(hass, result, name="Flat", role="auto")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await until(lambda: coordinator.connected)
    await until(lambda: hass.states.get("switch.plug") is not None)
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.plug"}, blocking=True
    )
    plug = sim.nodes[0x0002]
    assert plug.on
    assert len(seq_calls.runs) == 1  # the plug's provisioning run was observed

    # 2. A factory-reset lamp (with a Tuya vendor model) is added with Add device.
    radio.advertise_unprovisioned(SimUnprovisioned(LAMP_MAC, kind="ctl", name="Lamp"))
    result = await add_device(hass, entry, LAMP_MAC)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    lamp = sim.nodes[0x0003]
    assert len(seq_calls.runs) == 2  # and the lamp's
    # The lamp really has a vendor model to leave alone.
    assert any(
        TUYA_VENDOR_MODEL in element.vendor_models for element in lamp.composition().elements
    )
    await until(lambda: all(record.configured for record in entry.runtime_data.nodes.values()))
    await until(lambda: hass.states.get("light.lamp") is not None)
    await until(lambda: entry.runtime_data.connected)
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "light.lamp", ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    assert lamp.on
    assert hass.states.get("switch.plug").state == "on"

    # The invariants, over everything we sent.
    ours = [(pdu.ivi, pdu.seq) for pdu in sim.from_client if pdu.src == OUR_ADDRESS]
    assert ours and len(ours) == len(set(ours))  # no (IV, seq) reuse
    state = await NetworkStore(hass, entry.entry_id).async_load()
    assert max(seq for _ivi, seq in ours) < state["seq"]  # the saved ceiling covers every seq
    assert False in seq_calls.calls  # next_seq was called outside provisioning...
    assert True not in seq_calls.calls  # ...and never inside a provisioning run
    for node in (plug, lamp):
        assert not any(model == TUYA_VENDOR_MODEL for _element, model in node.bindings)
    await hass.config_entries.async_unload(entry.entry_id)
