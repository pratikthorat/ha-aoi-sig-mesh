"""One GATT proxy connection into a mesh network: filter, access messages, responses."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import LinkClosedError, MeshError, MeshTimeout
from .models import parse_opcode
from .network import MeshNetwork
from .pdu import (
    FILTER_ACCEPT,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    FilterStatus,
    ProxyReassembler,
    add_addresses,
    decrypt_network_pdu,
    decrypt_proxy_config,
    encrypt_network_pdu,
    encrypt_proxy_config,
    parse_filter_status,
    proxy_pdus,
    set_filter_type,
)
from .transport import (
    MAX_UNSEGMENTED_ACCESS,
    SegmentReassembler,
    decrypt_upper,
    encrypt_upper,
    make_segment_ack,
    parse_lower_access,
    unsegmented_access,
)

_LOGGER = logging.getLogger(__name__)

# Read at call time, so tests can shorten them with monkeypatch.
DEFAULT_TTL = 5
REQUEST_TIMEOUT = 2.0
REQUEST_ATTEMPTS = 3
FILTER_TIMEOUT = 2.0

GROUP_ADDRESS_MIN = 0xC000  # group addresses, including all-nodes 0xFFFF


class GattTransport(Protocol):
    """The GATT link to a proxy node (Mesh Proxy Data In / Data Out)."""

    max_write: int  # the largest write the link takes (ATT MTU - 3)

    @property
    def is_connected(self) -> bool: ...

    def set_notify_callback(self, callback: Callable[[bytes], None]) -> None: ...

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class AccessMessage:
    """One decrypted access message from the mesh."""

    src: int
    dst: int
    opcode: int
    params: bytes
    dev_key: bool  # True when it was encrypted with the node's DevKey (Config messages)


class ProxyLink:
    """One proxy connection: sends access messages, matches responses, dispatches the rest.

    ``on_message`` sees every access message that decrypts, including responses to
    requests. Callbacks run in the event loop and must not block.
    """

    def __init__(
        self,
        network: MeshNetwork,
        transport: GattTransport,
        *,
        dev_key_for: Callable[[int], bytes | None],
        on_message: Callable[[AccessMessage], None] | None = None,
        on_close: Callable[[], None] | None = None,
        ttl: int | None = None,
    ) -> None:
        self._network = network
        self._transport = transport
        self._dev_key_for = dev_key_for
        self._on_message = on_message
        self._on_close = on_close
        self._ttl = DEFAULT_TTL if ttl is None else ttl
        self._proxy_rx = ProxyReassembler()
        self._segments = SegmentReassembler()
        self._waiters: dict[tuple[int, int], list[asyncio.Future[AccessMessage]]] = {}
        self._filter_event = asyncio.Event()
        self._filter_status: FilterStatus | None = None
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self.filter_ok = False
        transport.set_notify_callback(self._on_notify)
        transport.set_disconnect_callback(self._on_disconnect)

    @property
    def is_connected(self) -> bool:
        return not self._closed and self._transport.is_connected

    # public API

    async def start(self, *, use_filter: bool = True, filter_timeout: float | None = None) -> None:
        """Set the proxy filter to an accept list holding our address.

        A proxy that does not answer is tolerated: the filter is optional, and proxies add
        the src of every network PDU we send to their accept list anyway.
        """
        self.filter_ok = False
        if not use_filter:
            return
        timeout = FILTER_TIMEOUT if filter_timeout is None else filter_timeout
        await self._filter_step(set_filter_type(FILTER_ACCEPT), timeout)
        status = await self._filter_step(add_addresses(self._network.address), timeout)
        self.filter_ok = (
            status is not None and status.filter_type == FILTER_ACCEPT and status.list_size >= 1
        )
        if not self.filter_ok:
            _LOGGER.debug("Proxy did not confirm the filter; relying on its default")

    async def send(self, dst: int, access: bytes, *, dev_key: bytes | None = None) -> None:
        """Send one access message, with the AppKey unless ``dev_key`` is given."""
        network = self._network
        akf = 0 if dev_key else 1
        key = dev_key or network.app_key
        aid = 0 if dev_key else network.aid
        if len(access) > MAX_UNSEGMENTED_ACCESS:
            raise MeshError(f"{len(access)}-byte access message needs segmentation")
        # No await between taking the seq and queueing on the (FIFO) write lock, so PDUs
        # leave in seq order: nodes drop an older seq that arrives after a newer one.
        seq = network.next_seq()
        iv_index = network.tx_iv_index
        upper = encrypt_upper(
            key,
            akf=akf,
            szmic=0,
            seq=seq,
            src=network.address,
            dst=dst,
            iv_index=iv_index,
            access_payload=access,
        )
        lower = unsegmented_access(upper, akf=akf, aid=aid)
        await self._send_network(dst, lower, seq=seq, iv_index=iv_index)

    async def request(
        self,
        dst: int,
        access: bytes,
        response_opcode: int,
        *,
        dev_key: bytes | None = None,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> AccessMessage:
        """Send ``access`` to ``dst`` and wait for ``response_opcode`` from ``dst``."""
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        attempts = REQUEST_ATTEMPTS if attempts is None else attempts
        key = (dst, response_opcode)
        future: asyncio.Future[AccessMessage] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(future)
        try:
            for _ in range(attempts):
                await self.send(dst, access, dev_key=dev_key)
                try:
                    return await asyncio.wait_for(asyncio.shield(future), timeout)
                except TimeoutError:
                    continue
            raise MeshTimeout(
                f"no 0x{response_opcode:04X} from 0x{dst:04X} after {attempts} attempts"
            )
        finally:
            waiters = self._waiters.get(key, [])
            if future in waiters:
                waiters.remove(future)
            if not waiters:
                self._waiters.pop(key, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # mark a link-closed error as retrieved

    async def close(self) -> None:
        """Close the link on purpose. ``on_close`` is not called."""
        self._handle_closed(notify=False)
        await self._transport.close()

    def forget_node(self, first: int, count: int) -> None:
        """Forget a reset and re-provisioned node's elements, so its restarted SEQ is accepted."""
        self._network.forget_replay(first, count)
        self._segments.forget(first, count)

    # sending

    async def _filter_step(self, message: bytes, timeout: float) -> FilterStatus | None:
        self._filter_event.clear()
        self._filter_status = None
        await self._send_proxy_config(message)
        try:
            await asyncio.wait_for(self._filter_event.wait(), timeout)
        except TimeoutError:
            return None
        return self._filter_status

    async def _send_proxy_config(self, message: bytes) -> None:
        network = self._network
        pdu = encrypt_proxy_config(
            network.keys,
            seq=network.next_seq(),
            src=network.address,
            iv_index=network.tx_iv_index,
            message=message,
        )
        await self._write(PDU_PROXY_CONFIG, pdu)

    async def _send_network(
        self, dst: int, transport_pdu: bytes, *, seq: int, iv_index: int, ctl: int = 0
    ) -> None:
        network = self._network
        pdu = encrypt_network_pdu(
            network.keys,
            ctl=ctl,
            ttl=self._ttl,
            seq=seq,
            src=network.address,
            dst=dst,
            transport_pdu=transport_pdu,
            iv_index=iv_index,
        )
        await self._write(PDU_NETWORK, pdu)

    async def _send_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        network = self._network
        ack = make_segment_ack(seq_zero, block_ack)
        await self._send_network(
            dst, ack, seq=network.next_seq(), iv_index=network.tx_iv_index, ctl=1
        )

    async def _write(self, pdu_type: int, payload: bytes) -> None:
        async with self._write_lock:
            for part in proxy_pdus(pdu_type, payload, self._transport.max_write):
                if not self.is_connected:
                    raise LinkClosedError("proxy link is closed")
                try:
                    await self._transport.write(part)
                except LinkClosedError:
                    raise
                except Exception as err:  # bleak and the HA bluetooth stack raise their own types
                    self._handle_closed(notify=True)
                    raise LinkClosedError(f"proxy write failed: {err}") from err

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a send from a notification callback, keeping a reference until it is done."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.debug("Background send failed: %s", task.exception())

    # receiving

    def _on_notify(self, data: bytes) -> None:
        """GATT notification callback. Never raises: junk from the radio is dropped."""
        if self._closed:
            return
        try:
            message = self._proxy_rx.feed(bytes(data))
            if message is None:
                return
            pdu_type, payload = message
            if pdu_type == PDU_NETWORK:
                self._on_network(payload)
            elif pdu_type == PDU_PROXY_CONFIG:
                self._on_proxy_config(payload)
        except Exception:
            _LOGGER.debug("Dropped a bad proxy notification", exc_info=True)

    def _rx_iv_index(self, payload: bytes) -> int | None:
        return self._network.rx_iv_index(payload[0] >> 7) if payload else None

    def _on_proxy_config(self, payload: bytes) -> None:
        iv_index = self._rx_iv_index(payload)
        if iv_index is None:
            return
        net = decrypt_proxy_config(self._network.keys, payload, iv_index)
        status = parse_filter_status(net.transport_pdu) if net is not None else None
        if status is not None:
            self._filter_status = status
            self._filter_event.set()

    def _on_network(self, payload: bytes) -> None:
        network = self._network
        iv_index = self._rx_iv_index(payload)
        if iv_index is None:
            return
        net = decrypt_network_pdu(network.keys, payload, iv_index)
        if net is None or net.src == network.address:
            return
        if net.dst != network.address and net.dst < GROUP_ADDRESS_MIN:
            return
        if not network.accept_replay(net.src, net.seq, iv_index):
            return
        if net.ctl:
            return  # no control messages we act on
        lower = parse_lower_access(net.transport_pdu)
        if not lower.seg:
            self._on_upper(net.src, net.dst, iv_index, lower.akf, lower.aid, 0, net.seq, lower.data)
            return
        result = self._segments.add(net.src, net.seq, lower, iv_index=iv_index)
        if result.send_ack and net.dst == network.address:
            self._spawn(self._send_ack(net.src, result.seq_zero, result.block_ack))
        done = result.message
        if done is not None:
            self._on_upper(
                net.src,
                net.dst,
                done.iv_index,  # SeqAuth's IV: the IV index of the first segment
                done.akf,
                done.aid,
                done.szmic,
                done.seq_auth,
                done.upper,
            )

    def _on_upper(
        self,
        src: int,
        dst: int,
        iv_index: int,
        akf: int,
        aid: int,
        szmic: int,
        seq: int,
        upper: bytes,
    ) -> None:
        if akf:
            if aid != self._network.aid:
                return
            key = self._network.app_key
        else:
            key = self._dev_key_for(src)
            if key is None:
                return
        access = decrypt_upper(
            key, akf=akf, szmic=szmic, seq=seq, src=src, dst=dst, iv_index=iv_index, upper=upper
        )
        if access is None:
            return
        opcode, params = parse_opcode(access)
        self._dispatch(AccessMessage(src, dst, opcode, params, dev_key=not akf))

    def _dispatch(self, message: AccessMessage) -> None:
        for future in list(self._waiters.get((message.src, message.opcode), ())):
            if not future.done():
                future.set_result(message)
        if self._on_message is not None:
            try:
                self._on_message(message)
            except Exception:
                _LOGGER.exception("on_message callback failed for %s", message)

    def _on_disconnect(self) -> None:
        self._handle_closed(notify=True)

    def _handle_closed(self, *, notify: bool) -> None:
        """Fail everything that waits on this link. Runs once; later calls do nothing."""
        if self._closed:
            return
        self._closed = True
        for waiters in self._waiters.values():
            for future in waiters:
                if not future.done():
                    future.set_exception(LinkClosedError("proxy link closed"))
        self._filter_event.set()
        for task in self._tasks:
            task.cancel()
        if notify and self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                _LOGGER.exception("on_close callback failed")
