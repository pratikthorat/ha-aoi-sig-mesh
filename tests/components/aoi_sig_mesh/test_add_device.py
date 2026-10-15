"""Add device: provision a factory-reset device into the network, then set it up."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aoi_sig_mesh import provision
from custom_components.aoi_sig_mesh.const import DOMAIN, SUBENTRY_NODE
from custom_components.aoi_sig_mesh.mesh import provisioner
from custom_components.aoi_sig_mesh.mesh.errors import MeshError
from custom_components.aoi_sig_mesh.store import AddressBook, NetworkStore
from tests.sim.mesh_sim import SimMesh
from tests.sim.prov_sim import SimUnprovisioned
from tests.sim.sim_radio import SimRadio

from .conftest import NETWORK_ID, make_entry, until

PLUG_MAC = "DC:23:50:00:00:27"
LAMP_MAC = "DC:23:50:00:00:28"


@pytest.fixture(autouse=True)
def fast_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CAPABILITIES_TIMEOUT", "PUBLIC_KEY_TIMEOUT", "COMPLETE_TIMEOUT"):
        monkeypatch.setattr(provisioner, name, 0.2)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)


async def open_add(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_NODE), context={"source": SOURCE_USER}
    )


async def add_device(
    hass: HomeAssistant, entry: Any, address: str, role: str = "auto"
) -> dict[str, Any]:
    """Pick ``address`` in Add device and run provisioning to its end."""
    result = await open_add(hass, entry)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device": address, "role": role}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "provision"
    await hass.async_block_till_done()
    return await hass.config_entries.subentries.async_configure(result["flow_id"])


async def test_nothing_in_range_aborts(hass: HomeAssistant, bulb_entry: Any) -> None:
    result = await open_add(hass, bulb_entry)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "no_devices")


async def test_the_form_lists_devices_strongest_first_with_name_and_signal(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"), rssi=-80)
    radio.advertise_unprovisioned(SimUnprovisioned(LAMP_MAC), rssi=-50)

    result = await open_add(hass, bulb_entry)

    schema = result["data_schema"].schema
    (device_key, role_key) = list(schema)
    options = schema[device_key].config["options"]
    assert [(o["value"], o["label"]) for o in options] == [
        (LAMP_MAC, f"Unnamed device ({LAMP_MAC}, -50 dBm)"),
        (PLUG_MAC, f"Plug ({PLUG_MAC}, -80 dBm)"),
    ]
    assert schema[role_key].config["options"] == ["auto", "light", "switch"]
    assert role_key.default() == "auto"


async def test_a_factory_reset_plug_is_added_set_up_and_switched(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))

    result = await add_device(hass, bulb_entry, PLUG_MAC)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(s for s in bulb_entry.subentries.values() if s.title == "Plug")
    assert subentry.unique_id == "0002"  # after our 0x0001; the bulb is at 0x00B0
    assert subentry.data["mac"] == PLUG_MAC
    assert subentry.data["configured"] is False
    assert subentry.data["role"] is None
    assert sim.nodes[0x0002].dev_key.hex() == subentry.data["dev_key"]
    assert await AddressBook(hass).async_used_nodes(NETWORK_ID) == [(0x0002, 1)]

    plug = sim.nodes[0x0002]
    await until(lambda: bulb_entry.subentries[subentry.subentry_id].data["configured"])
    await until(lambda: hass.states.get("switch.plug") is not None)
    assert bulb_entry.subentries[subentry.subentry_id].data["role"] == "switch"
    await until(lambda: bulb_entry.runtime_data.connected)
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.plug"}, blocking=True
    )
    assert plug.on


@pytest.mark.parametrize(("role", "expected"), [("auto", "light"), ("switch", "switch")])
async def test_the_role_follows_the_node_unless_one_was_chosen(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any, role: str, expected: str
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(LAMP_MAC, kind="ctl", name="Lamp"))

    await add_device(hass, bulb_entry, LAMP_MAC, role)

    subentry = next(s for s in bulb_entry.subentries.values() if s.title == "Lamp")
    await until(lambda: bulb_entry.subentries[subentry.subentry_id].data["configured"])
    await until(lambda: hass.states.get(f"{expected}.lamp") is not None)
    assert bulb_entry.subentries[subentry.subentry_id].data["role"] == expected
    assert subentry.data["element_count"] == 2


@pytest.mark.parametrize(
    ("switch", "remote", "error", "step"),
    [
        ({"stall_at": "public_key"}, False, "provision_failed", "public_key"),
        ({"bad_confirmation": True}, False, "provision_failed", "random"),
        ({"stall_at": "capabilities"}, True, "provision_failed_remote", "capabilities"),
        ({"oob_only": True}, False, "oob_required", "capabilities"),
    ],
)
async def test_a_failure_goes_back_to_the_form_with_the_step(
    hass: HomeAssistant,
    sim: SimMesh,
    radio: SimRadio,
    bulb_entry: Any,
    switch: dict[str, Any],
    remote: bool,
    error: str,
    step: str,
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, **switch), remote=remote)

    result = await add_device(hass, bulb_entry, PLUG_MAC)

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["step"] == step
    assert result["description_placeholders"]["detail"]  # never an empty reason
    assert len(bulb_entry.subentries) == 1
    assert 0x0002 not in sim.nodes


async def test_a_failed_save_goes_back_to_the_form_as_cannot_save(
    hass: HomeAssistant,
    radio: SimRadio,
    bulb_entry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_save(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(AddressBook, "async_reserve_node", failing_save)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))

    result = await add_device(hass, bulb_entry, PLUG_MAC)

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    assert result["errors"] == {"base": "cannot_save"}
    assert len(bulb_entry.subentries) == 1


async def test_a_half_provisioned_device_must_be_reset_and_keeps_its_address(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug", stall_at="complete"))

    result = await add_device(hass, bulb_entry, PLUG_MAC)

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "half_provisioned")
    assert result["description_placeholders"] == {"name": "Plug", "step": "complete"}
    assert len(bulb_entry.subentries) == 1

    # reset and added again: a new address, never the one the half-done device may hold
    radio.forget(PLUG_MAC)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))
    result = await add_device(hass, bulb_entry, PLUG_MAC)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    added = next(s for s in bulb_entry.subentries.values() if s.title == "Plug")
    assert added.unique_id == "0003"


async def test_an_unloaded_network_gives_the_saved_iv_index(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    await NetworkStore(hass, entry.entry_id).async_save(
        {"iv_index": 7, "iv_update": True, "seq": 0, "replay": {}}
    )
    device = SimUnprovisioned(PLUG_MAC)
    radio.advertise_unprovisioned(device)

    result = await add_device(hass, entry, PLUG_MAC)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert device.provisioned.iv_index == 7
    assert device.provisioned.flags == provisioner.FLAG_IV_UPDATE


async def test_a_new_device_starts_with_a_clean_replay_entry(
    hass: HomeAssistant, sim: SimMesh, radio: SimRadio, bulb_entry: Any
) -> None:
    """A device deleted while the network was off can leave its seq in the replay cache; the
    new device at that address starts again at seq 0 and must still be heard."""
    assert bulb_entry.runtime_data.network.accept_replay(0x0002, 5000, 0)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))

    await add_device(hass, bulb_entry, PLUG_MAC)

    subentry = next(s for s in bulb_entry.subentries.values() if s.title == "Plug")
    await until(lambda: bulb_entry.subentries[subentry.subentry_id].data["configured"])
    _iv, seq = bulb_entry.runtime_data.network.persistent_state()["replay"]["2"]
    assert seq < 5000  # the plug's own restarted seq


async def pick(hass: HomeAssistant, flow_id: str, address: str) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_configure(
        flow_id, {"device": address, "role": "auto"}
    )


async def test_a_full_range_reaches_the_form_as_no_address(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def full(*_args: Any, **_kwargs: Any) -> int:
        raise MeshError("no free address")

    monkeypatch.setattr(AddressBook, "async_reserve_node", full)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))

    result = await add_device(hass, bulb_entry, PLUG_MAC)

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    assert result["errors"] == {"base": "no_address"}
    assert result["description_placeholders"]["detail"]


async def test_an_unexpected_error_returns_to_the_form_as_unknown(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("surprise")

    monkeypatch.setattr(provision, "async_provision_device", boom)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, name="Plug"))

    result = await open_add(hass, bulb_entry)
    # the task fails at once (HA starts tasks eagerly), so there may be no progress to wait on
    result = await pick(hass, result["flow_id"], PLUG_MAC)
    await hass.async_block_till_done()
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        result = await hass.config_entries.subentries.async_configure(result["flow_id"])

    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    assert result["errors"] == {"base": "unknown"}
    assert hass.data[DOMAIN]["provisioning"] == set()


async def test_a_failure_is_not_lost_when_the_device_is_gone(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, stall_at="public_key"))
    result = await open_add(hass, bulb_entry)
    result = await pick(hass, result["flow_id"], PLUG_MAC)
    radio.forget(PLUG_MAC)  # it left while we waited
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_configure(result["flow_id"])

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "provision_failed")
    assert result["description_placeholders"]["step"] == "public_key"


async def test_the_retry_form_keeps_the_same_device_when_its_signal_changes(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any
) -> None:
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, stall_at="public_key"), rssi=-30)
    radio.advertise_unprovisioned(SimUnprovisioned(LAMP_MAC), rssi=-40)
    result = await open_add(hass, bulb_entry)
    result = await pick(hass, result["flow_id"], PLUG_MAC)
    await hass.async_block_till_done()
    radio._unprovisioned[PLUG_MAC] = (radio._unprovisioned[PLUG_MAC][0], -70, False)

    result = await hass.config_entries.subentries.async_configure(result["flow_id"])

    assert result["step_id"] == "user"
    device_key = next(iter(result["data_schema"].schema))
    assert device_key.default() == PLUG_MAC  # not the lamp, which is now the strongest


async def test_a_device_being_added_in_another_window_is_refused(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provisioner, "PUBLIC_KEY_TIMEOUT", 30.0)
    radio.advertise_unprovisioned(SimUnprovisioned(PLUG_MAC, stall_at="public_key"))
    first = await open_add(hass, bulb_entry)
    first = await pick(hass, first["flow_id"], PLUG_MAC)
    assert first["type"] is FlowResultType.SHOW_PROGRESS
    second = await open_add(hass, bulb_entry)

    second = await pick(hass, second["flow_id"], PLUG_MAC)

    assert (second["type"], second["step_id"]) == (FlowResultType.FORM, "user")
    assert second["errors"] == {"base": "already_provisioning"}
    hass.config_entries.subentries.async_abort(first["flow_id"])
    await hass.async_block_till_done()


async def test_closing_the_dialog_cancels_provisioning_and_frees_the_device(
    hass: HomeAssistant, radio: SimRadio, bulb_entry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provisioner, "PUBLIC_KEY_TIMEOUT", 30.0)
    device = SimUnprovisioned(PLUG_MAC, stall_at="public_key")
    radio.advertise_unprovisioned(device)
    result = await open_add(hass, bulb_entry)
    result = await pick(hass, result["flow_id"], PLUG_MAC)
    await until(lambda: radio.provisioning_connects)
    assert hass.data[DOMAIN]["provisioning"] == {PLUG_MAC}

    hass.config_entries.subentries.async_abort(result["flow_id"])

    await until(lambda: not device._transport.is_connected)
    await until(lambda: hass.data[DOMAIN]["provisioning"] == set())
