"""Home Assistant Bluetooth as a mesh.connection.MeshRadio."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import bleak_retry_connector
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import PROVISIONING_SERVICE_UUID, PROXY_SERVICE_UUID
from .gatt import BleakGattTransport
from .mesh.connection import ProxyCandidate
from .mesh.errors import MeshError
from .mesh.provisioner import UnprovisionedDevice, parse_provisioning_service_data

CONNECT_ATTEMPTS = 3


def candidate_from_info(info: bluetooth.BluetoothServiceInfoBleak) -> ProxyCandidate:
    return ProxyCandidate(
        address=info.address.upper(),
        name=info.name or None,
        rssi=info.rssi,
        service_data=info.service_data.get(PROXY_SERVICE_UUID),
        seen=info.time,
    )


def _is_proxy(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    return PROXY_SERVICE_UUID in info.service_uuids or PROXY_SERVICE_UUID in info.service_data


class HaBluetoothRadio:
    """Adverts from every HA scanner (local adapter and ESPHome proxies); bleak connections
    through ``establish_connection``, which follows HA's connection-slot rules."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def proxies(self) -> list[ProxyCandidate]:
        return [
            candidate_from_info(info)
            for info in bluetooth.async_discovered_service_info(self._hass, connectable=True)
            if _is_proxy(info)
        ]

    def last_seen(self, address: str) -> ProxyCandidate | None:
        info = bluetooth.async_last_service_info(self._hass, address, connectable=False)
        return candidate_from_info(info) if info is not None else None

    def subscribe(self, callback_: Callable[[ProxyCandidate], None]) -> Callable[[], None]:
        @callback
        def _on_advert(
            info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
        ) -> None:
            callback_(candidate_from_info(info))

        return bluetooth.async_register_callback(
            self._hass,
            _on_advert,
            bluetooth.BluetoothCallbackMatcher(service_uuid=PROXY_SERVICE_UUID, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    def unprovisioned(self) -> list[UnprovisionedDevice]:
        """Factory-reset nodes advertising 0x1827, strongest first."""
        found = []
        for info in bluetooth.async_discovered_service_info(self._hass, connectable=True):
            data = info.service_data.get(PROVISIONING_SERVICE_UUID)
            parsed = parse_provisioning_service_data(data) if data is not None else None
            if parsed is None:
                continue
            scanner = bluetooth.async_scanner_by_source(self._hass, info.source)
            found.append(
                UnprovisionedDevice(
                    address=info.address.upper(),
                    name=info.name or None,
                    rssi=info.rssi,
                    uuid=parsed[0],
                    oob_info=parsed[1],
                    remote=isinstance(scanner, bluetooth.BaseHaRemoteScanner),
                )
            )
        found.sort(key=lambda d: -999 if d.rssi is None else d.rssi, reverse=True)
        return found

    async def connect(self, address: str) -> BleakGattTransport:
        return await self._connect(address, BleakGattTransport(), f"AoI mesh proxy {address}")

    async def connect_provisioning(self, address: str) -> BleakGattTransport:
        """A PB-GATT connection: separate from the network's proxy link, never cached."""
        return await self._connect(
            address,
            BleakGattTransport.provisioning(),
            f"AoI mesh provisioning {address}",
            use_services_cache=False,
        )

    async def _connect(
        self,
        address: str,
        transport: BleakGattTransport,
        name: str,
        *,
        use_services_cache: bool = True,
    ) -> BleakGattTransport:
        device = bluetooth.async_ble_device_from_address(self._hass, address, connectable=True)
        if device is None:
            raise MeshError(f"{address} is not in range of any connectable Bluetooth adapter")
        client = await establish_connection(
            # Looked up now: HA's bluetooth manager swaps this attribute for its own wrapper.
            bleak_retry_connector.BleakClientWithServiceCache,
            device,
            name,
            disconnected_callback=transport.handle_disconnect,
            max_attempts=CONNECT_ATTEMPTS,
            use_services_cache=use_services_cache,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(self._hass, address, connectable=True)
                or device
            ),
        )
        try:
            await transport.async_attach(client)
        except BaseException:  # includes CancelledError: ConnectionManager cancels connects
            # Shielded, so a second cancel cannot leave the client connected.
            await asyncio.shield(asyncio.ensure_future(transport.close()))
            raise
        return transport
