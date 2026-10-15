"""HaBluetoothRadio: adverts from HA's Bluetooth stack, connections through bleak."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import bluetooth as ha_bluetooth
from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh import gatt
from custom_components.aoi_sig_mesh.gatt import BleakGattTransport
from custom_components.aoi_sig_mesh.mesh.errors import MeshError
from custom_components.aoi_sig_mesh.mesh.provisioner import UnprovisionedDevice
from custom_components.aoi_sig_mesh.provision import async_unprovisioned
from custom_components.aoi_sig_mesh.radio import CONNECT_ATTEMPTS, HaBluetoothRadio
from tests.sim.mesh_sim import SimMesh

from .conftest import make_entry, node_data
from .test_gatt import FakeClient, FakeService, factory_reset_plug, tuya_bulb

BT = "custom_components.aoi_sig_mesh.radio.bluetooth"
PROXY = "00001828-0000-1000-8000-00805f9b34fb"
PROVISIONING = "00001827-0000-1000-8000-00805f9b34fb"
UUID = bytes(range(16))


def info(address: str, *, proxy: bool = True, rssi: int = -60) -> Any:
    return SimpleNamespace(
        address=address,
        name="Mesh CW",
        rssi=rssi,
        service_uuids=[PROXY] if proxy else [],
        service_data={PROXY: b"\x00" + bytes(8)} if proxy else {},
        time=100.0,
    )


async def test_lists_only_mesh_proxies(hass: HomeAssistant) -> None:
    adverts = [info("dc:23:50:00:00:19"), info("AA:BB:CC:DD:EE:FF", proxy=False)]
    with patch(f"{BT}.async_discovered_service_info", return_value=adverts):
        proxies = HaBluetoothRadio(hass).proxies()

    assert [proxy.address for proxy in proxies] == ["DC:23:50:00:00:19"]
    assert proxies[0].service_data == b"\x00" + bytes(8)
    assert proxies[0].rssi == -60
    assert proxies[0].seen == 100.0


async def test_last_seen_reads_any_scanner(hass: HomeAssistant) -> None:
    with patch(f"{BT}.async_last_service_info", return_value=None) as last:
        assert HaBluetoothRadio(hass).last_seen("DC:23:50:00:00:19") is None
    assert last.call_args.kwargs == {"connectable": False}


async def test_connect_needs_a_connectable_adapter_in_range(hass: HomeAssistant) -> None:
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=None),
        pytest.raises(MeshError),
    ):
        await HaBluetoothRadio(hass).connect("DC:23:50:00:00:19")


async def test_connect_attaches_the_proxy_characteristics(hass: HomeAssistant) -> None:
    client = tuya_bulb()
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=object()),
        patch(
            "custom_components.aoi_sig_mesh.radio.establish_connection",
            AsyncMock(return_value=client),
        ) as establish,
    ):
        transport = await HaBluetoothRadio(hass).connect("DC:23:50:00:00:19")

    assert isinstance(transport, BleakGattTransport)
    assert transport.is_connected
    assert establish.call_args.kwargs["disconnected_callback"] == transport.handle_disconnect
    assert establish.call_args.kwargs["max_attempts"] == CONNECT_ATTEMPTS
    assert callable(establish.call_args.kwargs["ble_device_callback"])


async def test_a_failed_attach_disconnects_the_client(hass: HomeAssistant) -> None:
    client = FakeClient(services=[FakeService("00007fdd-0000-1000-8000-00805f9b34fb", [])])
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=object()),
        patch(
            "custom_components.aoi_sig_mesh.radio.establish_connection",
            AsyncMock(return_value=client),
        ),
        pytest.raises(MeshError),
    ):
        await HaBluetoothRadio(hass).connect("DC:23:50:00:00:19")

    assert client.disconnects == 1


async def test_a_cancelled_connect_disconnects_the_client(hass: HomeAssistant) -> None:
    """ConnectionManager cancels in-flight connects; a connected client must not leak."""

    class HangingNotify(FakeClient):
        async def start_notify(self, char: Any, callback: Any) -> None:
            await asyncio.Event().wait()

    client = HangingNotify(services=tuya_bulb().services)
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=object()),
        patch(
            "custom_components.aoi_sig_mesh.radio.establish_connection",
            AsyncMock(return_value=client),
        ),
    ):
        task = asyncio.ensure_future(HaBluetoothRadio(hass).connect("DC:23:50:00:00:19"))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.disconnects == 1


async def test_a_provisioning_connect_whose_notify_hangs_fails_and_disconnects(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gatt, "WRITE_TIMEOUT", 0.05)
    client = factory_reset_plug()
    client.hang_notify = True
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=object()),
        patch(
            "custom_components.aoi_sig_mesh.radio.establish_connection",
            AsyncMock(return_value=client),
        ),
    ):
        async with asyncio.timeout(1):
            with pytest.raises(MeshError):
                await HaBluetoothRadio(hass).connect_provisioning("DC:23:50:00:00:27")

    assert client.disconnects == 1


async def test_subscribe_registers_a_passive_proxy_callback(hass: HomeAssistant) -> None:
    unsubscribe = MagicMock()
    with patch(f"{BT}.async_register_callback", return_value=unsubscribe) as register:
        result = HaBluetoothRadio(hass).subscribe(lambda candidate: None)

    assert result is unsubscribe
    args = register.call_args.args
    assert args[0] is hass
    assert args[2] == ha_bluetooth.BluetoothCallbackMatcher(service_uuid=PROXY, connectable=True)
    assert args[3] == ha_bluetooth.BluetoothScanningMode.PASSIVE
    seen = []
    with patch(f"{BT}.async_register_callback", return_value=unsubscribe) as register:
        HaBluetoothRadio(hass).subscribe(seen.append)
    register.call_args.args[1](info("dc:23:50:00:00:19"), None)
    assert [c.address for c in seen] == ["DC:23:50:00:00:19"]


def unprovisioned_info(
    address: str, *, data: bytes = UUID + b"\x00\x20", rssi: int = -70, source: str = "hci0"
) -> Any:
    return SimpleNamespace(
        address=address,
        name="Plug",
        rssi=rssi,
        service_uuids=[PROVISIONING],
        service_data={PROVISIONING: data},
        time=100.0,
        source=source,
    )


class FakeRemoteScanner(ha_bluetooth.BaseHaRemoteScanner):
    def __init__(self) -> None:  # no HA scanner state needed: only the type is checked
        pass


async def test_lists_unprovisioned_devices_strongest_first(hass: HomeAssistant) -> None:
    adverts = [
        unprovisioned_info("aa:00:00:00:00:01", rssi=-80),
        unprovisioned_info("aa:00:00:00:00:02", rssi=-50, source="esphome-proxy"),
        unprovisioned_info("aa:00:00:00:00:03", data=UUID),  # no OOB field: not valid
        info("dc:23:50:00:00:19"),
    ]
    scanners = {"hci0": object(), "esphome-proxy": FakeRemoteScanner()}
    with (
        patch(f"{BT}.async_discovered_service_info", return_value=adverts),
        patch(f"{BT}.async_scanner_by_source", side_effect=lambda hass, s: scanners.get(s)),
    ):
        devices = HaBluetoothRadio(hass).unprovisioned()

    assert devices == [
        UnprovisionedDevice("AA:00:00:00:00:02", "Plug", -50, UUID, 0x0020, remote=True),
        UnprovisionedDevice("AA:00:00:00:00:01", "Plug", -80, UUID, 0x0020, remote=False),
    ]


async def test_our_node_leaves_the_list_despite_its_merged_0x1827_entry(
    hass: HomeAssistant,
) -> None:
    """habluetooth merges service data across adverts from one address: a node we provisioned
    keeps its old 0x1827 entry next to the new 0x1828 one for as long as it advertises."""
    node = SimMesh().add_node("onoff", unicast=0x0002)
    entry = make_entry(("Plug", node_data(node, mac="AA:00:00:00:00:01")))
    entry.add_to_hass(hass)
    merged = unprovisioned_info("aa:00:00:00:00:01")
    merged.service_uuids = [PROVISIONING, PROXY]
    merged.service_data = {**merged.service_data, PROXY: b"\x00" + bytes(8)}
    adverts = [merged, unprovisioned_info("aa:00:00:00:00:02")]
    with (
        patch(f"{BT}.async_discovered_service_info", return_value=adverts),
        patch(f"{BT}.async_scanner_by_source", return_value=object()),
    ):
        assert [d.address for d in async_unprovisioned(hass)] == ["AA:00:00:00:00:02"]

        # deleted here (and so presumably reset): listed again
        hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
        assert [d.address for d in async_unprovisioned(hass)] == [
            "AA:00:00:00:00:01",
            "AA:00:00:00:00:02",
        ]


async def test_connect_provisioning_skips_the_service_cache(hass: HomeAssistant) -> None:
    client = factory_reset_plug()
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=object()),
        patch(
            "custom_components.aoi_sig_mesh.radio.establish_connection",
            AsyncMock(return_value=client),
        ) as establish,
    ):
        transport = await HaBluetoothRadio(hass).connect_provisioning("AA:00:00:00:00:01")

    assert transport.is_connected
    assert establish.call_args.kwargs["use_services_cache"] is False
    assert establish.call_args.kwargs["disconnected_callback"] == transport.handle_disconnect
    await transport.write(b"\x03\x00\x05")
    assert client.writes == [(0x30, b"\x03\x00\x05", False)]


async def test_connect_provisioning_needs_a_connectable_adapter(hass: HomeAssistant) -> None:
    with (
        patch(f"{BT}.async_ble_device_from_address", return_value=None),
        pytest.raises(MeshError),
    ):
        await HaBluetoothRadio(hass).connect_provisioning("AA:00:00:00:00:01")
