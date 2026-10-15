"""HaBluetoothRadio: adverts from HA's Bluetooth stack, connections through bleak."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh.gatt import BleakGattTransport
from custom_components.aoi_sig_mesh.mesh.errors import MeshError
from custom_components.aoi_sig_mesh.radio import HaBluetoothRadio

from .test_gatt import FakeClient, FakeService, tuya_bulb

BT = "custom_components.aoi_sig_mesh.radio.bluetooth"
PROXY = "00001828-0000-1000-8000-00805f9b34fb"


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
