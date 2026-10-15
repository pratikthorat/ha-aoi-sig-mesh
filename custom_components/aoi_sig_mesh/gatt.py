"""bleak-backed GattTransport for mesh.proxy.ProxyLink."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .const import PROVISIONING_SERVICE_UUID, PROXY_SERVICE_UUID
from .mesh.errors import LinkClosedError, MeshError

_LOGGER = logging.getLogger(__name__)

DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"
PROVISIONING_DATA_IN_UUID = "00002adb-0000-1000-8000-00805f9b34fb"
PROVISIONING_DATA_OUT_UUID = "00002adc-0000-1000-8000-00805f9b34fb"
DISCONNECT_TIMEOUT = 2.0  # close() must return even if the adapter never answers
CACHE_CLEAR_TIMEOUT = 2.0  # well under provisioner.CLOSE_TIMEOUT, which bounds all of close()
MIN_WRITE = 20  # ATT_MTU 23 - 3
WRITE_TIMEOUT = 5.0  # seconds, per write and for the notify enable; read at call time


class BleakGattTransport:
    """One bleak client. Characteristics are resolved under one service (0x1828 Mesh Proxy by
    default) and used by handle, because Tuya also exposes 2ADD/2ADE under its own 0x7FDD
    service.

    Create it first, pass ``handle_disconnect`` as bleak's disconnected callback, then
    ``async_attach`` the connected client.
    """

    def __init__(
        self,
        service_uuid: str = PROXY_SERVICE_UUID,
        data_in_uuid: str = DATA_IN_UUID,
        data_out_uuid: str = DATA_OUT_UUID,
        *,
        label: str = "Mesh Proxy",
        clear_cache_on_close: bool = False,
    ) -> None:
        self._service_uuid = service_uuid
        self._data_in_uuid = data_in_uuid
        self._data_out_uuid = data_out_uuid
        self._label = label
        self._clear_cache_on_close = clear_cache_on_close
        self._client: Any = None
        self._data_in: Any = None
        self._notify: Callable[[bytes], None] | None = None
        self._disconnect: Callable[[], None] | None = None
        self._closed = False
        self.max_write = MIN_WRITE

    @classmethod
    def provisioning(cls) -> BleakGattTransport:
        """PB-GATT: Data In 2ADB / Data Out 2ADC under the Mesh Provisioning Service (0x1827).

        The device's GATT table changes once it is provisioned (0x1827 goes, 0x1828 comes), so
        the service cache is cleared on close.
        """
        return cls(
            PROVISIONING_SERVICE_UUID,
            PROVISIONING_DATA_IN_UUID,
            PROVISIONING_DATA_OUT_UUID,
            label="Mesh Provisioning",
            clear_cache_on_close=True,
        )

    @property
    def is_connected(self) -> bool:
        return not self._closed and self._client is not None and bool(self._client.is_connected)

    def set_notify_callback(self, callback: Callable[[bytes], None]) -> None:
        self._notify = callback

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._disconnect = callback

    async def async_attach(self, client: Any) -> None:
        self._client = client  # set first, so close() disconnects it if a check below fails
        service = next(
            (s for s in client.services if str(s.uuid).lower() == self._service_uuid), None
        )
        if service is None:
            # Perhaps a table cached before the device was provisioned: drop it, so the next
            # connect discovers the services again.
            self._clear_cache_on_close = True
            raise MeshError(f"the device has no {self._label} service")
        chars = {str(c.uuid).lower(): c for c in service.characteristics}
        data_in, data_out = chars.get(self._data_in_uuid), chars.get(self._data_out_uuid)
        if data_in is None or data_out is None:
            raise MeshError(f"the {self._label} service lacks Data In/Out")
        self._data_in = data_in
        self.max_write = max(MIN_WRITE, int(data_in.max_write_without_response_size or 0))
        limit = WRITE_TIMEOUT
        try:
            # The notify enable is a CCCD write the device must answer.
            async with asyncio.timeout(limit) as deadline:
                await client.start_notify(data_out, self._on_notify)
        except TimeoutError as err:
            if not deadline.expired():
                raise
            raise MeshError(
                f"the {self._label} notifications did not start in {limit:g} s"
            ) from err
        _LOGGER.debug(
            "%s characteristics: in=%s out=%s", self._label, data_in.handle, data_out.handle
        )

    def handle_disconnect(self, client: Any) -> None:
        """bleak's disconnected callback. Callbacks from any other client are stale."""
        if client is not self._client or self._closed:
            return
        self._closed = True
        if self._disconnect is not None:
            self._disconnect()

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        if self._notify is not None and not self._closed:
            self._notify(bytes(data))

    async def write(self, data: bytes) -> None:
        """One GATT write, limited to WRITE_TIMEOUT.

        ProxyLink shields the writes of a split PDU from cancellation, so a write that hangs
        would hang the link for good. A TimeoutError makes ProxyLink close the link instead.
        """
        if not self.is_connected:
            raise LinkClosedError("bleak client is not connected")
        async with asyncio.timeout(WRITE_TIMEOUT):
            await self._client.write_gatt_char(self._data_in, data, response=False)

    async def close(self) -> None:
        self._closed = True
        client = self._client
        if client is None:
            return
        try:
            clear_cache = getattr(client, "clear_cache", None)
            if self._clear_cache_on_close and clear_cache is not None:
                try:
                    async with asyncio.timeout(CACHE_CLEAR_TIMEOUT):
                        await clear_cache()
                except Exception as err:  # closing must never raise
                    _LOGGER.debug("Ignoring error while clearing the service cache: %s", err)
        finally:
            # Always release the link, even if the caller cancels us during the cache clear.
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()
            except Exception as err:  # incl. TimeoutError; a cancel aimed at us still propagates
                _LOGGER.debug("Ignoring error while disconnecting: %s", err)
