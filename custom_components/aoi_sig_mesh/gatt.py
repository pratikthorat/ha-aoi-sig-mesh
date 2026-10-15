"""bleak-backed GattTransport for mesh.proxy.ProxyLink."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .const import PROXY_SERVICE_UUID
from .mesh.errors import LinkClosedError, MeshError

_LOGGER = logging.getLogger(__name__)

DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"
MIN_WRITE = 20  # ATT_MTU 23 - 3
WRITE_TIMEOUT = 5.0  # seconds; read at call time, so tests can shorten it


class BleakGattTransport:
    """One bleak client. Characteristics are resolved under service 0x1828 and used by handle,
    because Tuya also exposes 2ADD/2ADE under its own 0x7FDD service.

    Create it first, pass ``handle_disconnect`` as bleak's disconnected callback, then
    ``async_attach`` the connected client.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._data_in: Any = None
        self._notify: Callable[[bytes], None] | None = None
        self._disconnect: Callable[[], None] | None = None
        self._closed = False
        self.max_write = MIN_WRITE

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
            (s for s in client.services if str(s.uuid).lower() == PROXY_SERVICE_UUID), None
        )
        if service is None:
            raise MeshError("the device has no Mesh Proxy service")
        chars = {str(c.uuid).lower(): c for c in service.characteristics}
        data_in, data_out = chars.get(DATA_IN_UUID), chars.get(DATA_OUT_UUID)
        if data_in is None or data_out is None:
            raise MeshError("the Mesh Proxy service lacks Data In/Out")
        self._data_in = data_in
        self.max_write = max(MIN_WRITE, int(data_in.max_write_without_response_size or 0))
        await client.start_notify(data_out, self._on_notify)
        _LOGGER.debug("Proxy characteristics: in=%s out=%s", data_in.handle, data_out.handle)

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
            await client.disconnect()
        except Exception as err:  # closing must never raise
            _LOGGER.debug("Ignoring error while disconnecting: %s", err)
