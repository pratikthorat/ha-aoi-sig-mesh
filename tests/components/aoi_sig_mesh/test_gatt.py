"""BleakGattTransport against a fake bleak client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.aoi_sig_mesh import gatt
from custom_components.aoi_sig_mesh.gatt import (
    DATA_IN_UUID,
    DATA_OUT_UUID,
    PROVISIONING_DATA_IN_UUID,
    PROVISIONING_DATA_OUT_UUID,
    BleakGattTransport,
)
from custom_components.aoi_sig_mesh.mesh import provisioner
from custom_components.aoi_sig_mesh.mesh.errors import LinkClosedError, MeshError

TUYA_SERVICE_UUID = "00007fdd-0000-1000-8000-00805f9b34fb"
PROXY_SERVICE_UUID = "00001828-0000-1000-8000-00805f9b34fb"
PROVISIONING_SERVICE_UUID = "00001827-0000-1000-8000-00805f9b34fb"


@dataclass
class FakeChar:
    uuid: str
    handle: int
    max_write_without_response_size: int = 20


@dataclass
class FakeService:
    uuid: str
    characteristics: list[FakeChar]


@dataclass
class FakeClient:
    services: list[FakeService]
    is_connected: bool = True
    writes: list[tuple[int, bytes, bool]] = field(default_factory=list)
    notify: dict[int, Any] = field(default_factory=dict)
    disconnects: int = 0
    cache_clears: int = 0
    hang_clear_cache: bool = False
    hang_disconnect: bool = False
    fail_disconnect: bool = False
    hang_writes: bool = False
    hang_notify: bool = False

    async def start_notify(self, char: FakeChar, callback: Any) -> None:
        if self.hang_notify:
            await asyncio.Event().wait()  # the CCCD write is never answered
        self.notify[char.handle] = callback

    async def write_gatt_char(self, char: FakeChar, data: bytes, response: bool) -> None:
        if self.hang_writes:
            await asyncio.Event().wait()  # a stuck adapter: the write never returns
        self.writes.append((char.handle, bytes(data), response))

    async def clear_cache(self) -> bool:
        self.cache_clears += 1
        if self.hang_clear_cache:
            await asyncio.Event().wait()  # a slow proxy round trip: it never answers
        return True

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self.hang_disconnect:
            await asyncio.Event().wait()  # a stuck adapter: the disconnect never returns
        self.is_connected = False
        if self.fail_disconnect:
            raise OSError("adapter went away")


def tuya_bulb(mtu_write: int = 66) -> FakeClient:
    """Tuya exposes 2ADD/2ADE twice: under its own 0x7FDD service and under 0x1828."""
    return FakeClient(
        services=[
            FakeService(
                TUYA_SERVICE_UUID,
                [FakeChar(DATA_IN_UUID, 0x10), FakeChar(DATA_OUT_UUID, 0x12)],
            ),
            FakeService(
                PROXY_SERVICE_UUID,
                [FakeChar(DATA_IN_UUID, 0x20, mtu_write), FakeChar(DATA_OUT_UUID, 0x22)],
            ),
        ]
    )


async def test_uses_the_characteristics_under_the_proxy_service() -> None:
    client = tuya_bulb()
    transport = BleakGattTransport()
    received: list[bytes] = []
    transport.set_notify_callback(received.append)

    await transport.async_attach(client)
    await transport.write(b"\x00\x01")
    client.notify[0x22](None, bytearray(b"\x01\x02"))

    assert client.writes == [(0x20, b"\x00\x01", False)]
    assert received == [b"\x01\x02"]
    assert transport.max_write == 66
    assert transport.is_connected


def factory_reset_plug() -> FakeClient:
    """An unprovisioned node: Mesh Provisioning (0x1827) with Data In 2ADB / Data Out 2ADC."""
    return FakeClient(
        services=[
            FakeService(
                PROVISIONING_SERVICE_UUID,
                [
                    FakeChar(PROVISIONING_DATA_IN_UUID, 0x30, 20),
                    FakeChar(PROVISIONING_DATA_OUT_UUID, 0x32),
                ],
            )
        ]
    )


async def test_the_provisioning_transport_uses_2adb_and_2adc_under_1827() -> None:
    client = factory_reset_plug()
    transport = BleakGattTransport.provisioning()
    received: list[bytes] = []
    transport.set_notify_callback(received.append)

    await transport.async_attach(client)
    await transport.write(b"\x03\x00\x05")
    client.notify[0x32](None, bytearray(b"\x03\x01"))

    assert client.writes == [(0x30, b"\x03\x00\x05", False)]
    assert received == [b"\x03\x01"]


async def test_the_provisioning_transport_clears_the_service_cache_on_close() -> None:
    """After provisioning the device swaps 0x1827 for 0x1828: a cached table would be stale."""
    client = factory_reset_plug()
    transport = BleakGattTransport.provisioning()
    await transport.async_attach(client)

    await transport.close()

    assert (client.cache_clears, client.disconnects) == (1, 1)


async def test_a_proxy_without_0x1828_clears_the_cache_and_a_plug_without_1827_is_refused() -> None:
    proxy = BleakGattTransport()
    plug = factory_reset_plug()
    with pytest.raises(MeshError, match="Mesh Proxy"):
        await proxy.async_attach(plug)
    await proxy.close()
    assert plug.cache_clears == 1  # a stale table from before provisioning is dropped

    provisioning = BleakGattTransport.provisioning()
    with pytest.raises(MeshError, match="Mesh Provisioning"):
        await provisioning.async_attach(tuya_bulb())


async def test_an_unknown_write_size_falls_back_to_twenty_bytes() -> None:
    transport = BleakGattTransport()

    await transport.async_attach(tuya_bulb(mtu_write=0))

    assert transport.max_write == 20


async def test_a_device_without_the_proxy_service_is_refused_and_closed() -> None:
    client = FakeClient(services=[FakeService(TUYA_SERVICE_UUID, [])])
    transport = BleakGattTransport()

    with pytest.raises(MeshError):
        await transport.async_attach(client)
    await transport.close()

    assert client.disconnects == 1


async def test_disconnect_fires_once_and_ignores_stale_clients() -> None:
    client = tuya_bulb()
    transport = BleakGattTransport()
    drops: list[None] = []
    transport.set_disconnect_callback(lambda: drops.append(None))
    await transport.async_attach(client)

    transport.handle_disconnect(tuya_bulb())  # an older client's late callback
    assert drops == []
    transport.handle_disconnect(client)
    transport.handle_disconnect(client)

    assert drops == [None]
    assert not transport.is_connected
    with pytest.raises(LinkClosedError):
        await transport.write(b"\x00")


async def test_close_never_raises() -> None:
    client = tuya_bulb()
    client.fail_disconnect = True
    transport = BleakGattTransport()
    await transport.async_attach(client)

    await transport.close()
    await BleakGattTransport().close()  # never attached

    assert client.disconnects == 1


async def test_a_hung_write_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TimeoutError (not LinkClosedError) makes ProxyLink close the link."""
    monkeypatch.setattr(gatt, "WRITE_TIMEOUT", 0.05)
    client = tuya_bulb()
    client.hang_writes = True
    transport = BleakGattTransport()
    await transport.async_attach(client)

    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await transport.write(b"\x00\x01")


@pytest.mark.parametrize("make_client", [tuya_bulb, factory_reset_plug])
async def test_a_hung_notify_enable_fails_the_attach(
    monkeypatch: pytest.MonkeyPatch, make_client: Any
) -> None:
    """A proxy that never answers the notify enable must not hang the connect."""
    monkeypatch.setattr(gatt, "WRITE_TIMEOUT", 0.05)
    client = make_client()
    client.hang_notify = True
    transport = (
        BleakGattTransport.provisioning()
        if make_client is factory_reset_plug
        else BleakGattTransport()
    )

    async with asyncio.timeout(1):
        with pytest.raises(MeshError, match="notifications did not start in 0.05 s"):
            await transport.async_attach(client)


async def test_a_notification_after_close_is_dropped() -> None:
    client = tuya_bulb()
    transport = BleakGattTransport()
    received: list[bytes] = []
    transport.set_notify_callback(received.append)
    await transport.async_attach(client)

    await transport.close()
    client.notify[0x22](None, bytearray(b"\x01"))

    assert received == []


async def test_close_still_disconnects_when_an_outer_timeout_cancels_the_cache_clear() -> None:
    """close() alone: an outer bound firing during the cache clear must not skip disconnect()."""
    assert gatt.CACHE_CLEAR_TIMEOUT < provisioner.CLOSE_TIMEOUT
    client = factory_reset_plug()
    client.hang_clear_cache = True
    transport = BleakGattTransport.provisioning()
    await transport.async_attach(client)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):  # the outer bound fires before the cache clear's own
            await transport.close()

    assert client.disconnects == 1


async def test_provision_device_still_releases_the_link_when_the_cache_clear_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real close path (provisioner.provision_device's CLOSE_TIMEOUT) with a stuck clear."""
    monkeypatch.setattr(provisioner, "CAPABILITIES_TIMEOUT", 0.1)
    monkeypatch.setattr(provisioner, "START_DELAY", 0)
    monkeypatch.setattr(provisioner, "CLOSE_TIMEOUT", 0.3)
    monkeypatch.setattr(gatt, "CACHE_CLEAR_TIMEOUT", 5.0)  # the outer bound must be what fires
    client = factory_reset_plug()
    client.hang_clear_cache = True

    class Radio:
        async def connect_provisioning(self, address: str) -> BleakGattTransport:
            transport = BleakGattTransport.provisioning()
            await transport.async_attach(client)
            return transport

    async def assign(caps: Any) -> int:
        return 2

    with pytest.raises(provisioner.ProvisioningError) as caught:  # the device never answers
        async with asyncio.timeout(2):  # bounded: a hang here would be a stall
            await provisioner.provision_device(
                Radio(),
                "AA:00:00:00:00:01",
                net_key=bytes(16),
                net_key_index=0,
                iv_index=0,
                assign=assign,
            )

    assert caught.value.step == "capabilities"
    assert client.disconnects == 1


async def test_a_hung_disconnect_is_bounded_even_after_an_outer_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gatt, "DISCONNECT_TIMEOUT", 0.1)
    client = factory_reset_plug()
    client.hang_clear_cache = True
    client.hang_disconnect = True
    transport = BleakGattTransport.provisioning()
    await transport.async_attach(client)

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):  # the outer bound, spent during the cache clear
        async with asyncio.timeout(0.05):
            await transport.close()

    assert asyncio.get_running_loop().time() - started < 1
    assert client.disconnects == 1
