"""Home Assistant Bluetooth as a mesh.connection.MeshRadio."""

from __future__ import annotations

from collections.abc import Callable

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import PROXY_SERVICE_UUID
from .gatt import BleakGattTransport
from .mesh.connection import ProxyCandidate
from .mesh.errors import MeshError

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

    async def connect(self, address: str) -> BleakGattTransport:
        device = bluetooth.async_ble_device_from_address(self._hass, address, connectable=True)
        if device is None:
            raise MeshError(f"{address} is not in range of any connectable Bluetooth adapter")
        transport = BleakGattTransport()
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            f"AoI mesh proxy {address}",
            disconnected_callback=transport.handle_disconnect,
            max_attempts=CONNECT_ATTEMPTS,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(self._hass, address, connectable=True)
                or device
            ),
        )
        try:
            await transport.async_attach(client)
        except Exception:
            await transport.close()
            raise
        return transport
