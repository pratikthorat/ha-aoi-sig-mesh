"""One proxy connection per network: proxy choice, reconnect loop with backoff, idle policy.

Home Assistant supplies a ``MeshRadio`` (adverts + bleak connections); tests supply a fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from .errors import LinkClosedError, MeshTimeout
from .network import BeaconOutcome, MeshNetwork
from .pdu import SecureBeacon
from .proxy import AccessMessage, GattTransport, ProxyLink, parse_proxy_service_data

_LOGGER = logging.getLogger(__name__)

# Read at call time, so tests can shorten them with monkeypatch.
BACKOFF_MIN = 2.0
BACKOFF_MAX = 60.0
CONNECT_TIMEOUT = 60.0
CLOSE_TIMEOUT = 5.0


@dataclass(frozen=True)
class ProxyCandidate:
    """A node advertising the Mesh Proxy service (0x1828)."""

    address: str  # Bluetooth MAC, upper case
    name: str | None
    rssi: int | None
    service_data: bytes | None  # 0x1828 service data
    seen: float  # time.monotonic() of the advert


class MeshRadio(Protocol):
    def proxies(self) -> list[ProxyCandidate]: ...

    def last_seen(self, address: str) -> ProxyCandidate | None: ...

    def subscribe(self, callback: Callable[[ProxyCandidate], None]) -> Callable[[], None]: ...

    async def connect(self, address: str) -> GattTransport: ...


class ConnectionManager:
    """Keeps one ProxyLink up (or down while idle) and reports connects and drops."""

    def __init__(
        self,
        network: MeshNetwork,
        radio: MeshRadio,
        *,
        dev_key_for: Callable[[int], bytes | None],
        node_addresses: Callable[[], set[str]],
        on_connected: Callable[[ProxyLink, ProxyCandidate], Awaitable[None]],
        on_disconnected: Callable[[], None],
        on_message: Callable[[AccessMessage], None] | None = None,
        on_beacon: Callable[[SecureBeacon, BeaconOutcome], None] | None = None,
        use_filter: bool = True,
        idle_timeout: float = 0.0,
    ) -> None:
        self._network = network
        self._radio = radio
        self._dev_key_for = dev_key_for
        self._node_addresses = node_addresses
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_message = on_message
        self._on_beacon = on_beacon
        self._use_filter = use_filter
        self._idle_timeout = idle_timeout
        self._link: ProxyLink | None = None
        self._proxy: ProxyCandidate | None = None
        self._generation = 0
        self._closed = asyncio.Event()
        self._connected = asyncio.Event()
        self._wake = asyncio.Event()
        self._demand = asyncio.Event()
        self._tried: set[str] = set()
        self._skip_delay = False
        self._connecting = False
        self._reconnect_pending = False
        self._idle = False
        self._last_activity = 0.0
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None

    # public

    @property
    def link(self) -> ProxyLink | None:
        link = self._link
        return link if link is not None and link.is_connected else None

    @property
    def proxy(self) -> ProxyCandidate | None:
        return self._proxy if self.link is not None else None

    @property
    def idle(self) -> bool:
        """True while disconnected on purpose by the idle policy."""
        return self._idle

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._demand.set()  # connect once at start-up to read every node's state
        self._unsubscribe = self._radio.subscribe(self._on_advert)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="aoi_sig_mesh link")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.exception("Mesh connection loop had failed")

    def matches(self, candidate: ProxyCandidate) -> bool:
        """Our Network ID in the advert, or the MAC of a node we know (Node Identity adverts)."""
        if candidate.service_data:
            advert = parse_proxy_service_data(candidate.service_data)
            if advert is not None and advert.network_id == self._network.keys.network_id:
                return True
        return candidate.address.upper() in self._node_addresses()

    def candidates(self) -> list[ProxyCandidate]:
        found = [c for c in self._radio.proxies() if self.matches(c)]
        found.sort(key=lambda c: -999 if c.rssi is None else c.rssi, reverse=True)
        return found

    def touch(self) -> None:
        """Note activity, so the idle policy keeps the link a little longer."""
        self._last_activity = asyncio.get_running_loop().time()

    async def get_link(self, timeout: float) -> ProxyLink:
        """The connected link, connecting on demand. Raises MeshTimeout if none comes up."""
        self.touch()
        link = self.link
        if link is not None:
            return link
        self._demand.set()
        self._wake.set()
        try:
            async with asyncio.timeout(timeout):
                while (link := self.link) is None:
                    await self._connected.wait()
                    if self.link is None:
                        await asyncio.sleep(0.05)  # the old link is still being torn down
        except TimeoutError as err:
            raise MeshTimeout("no mesh proxy connection") from err
        return link

    def reconnect(self) -> None:
        """Drop the current link (if any) and try again right away."""
        self._skip_delay = True
        if self._connecting:
            self._reconnect_pending = True  # the link being set up is dropped once it is up
        self._demand.set()
        self._closed.set()
        self._wake.set()

    # loop

    async def _run(self) -> None:
        backoff = BACKOFF_MIN
        while True:
            try:
                backoff = await self._cycle(backoff)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Mesh connection loop failed; retrying")
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _cycle(self, backoff: float) -> float:
        """One connect-and-serve round. Returns the backoff for the next round."""
        loop = asyncio.get_running_loop()
        if self._idle_timeout > 0 and not self._demand.is_set():
            self._idle = True
            await self._demand.wait()
        self._idle = False
        self._skip_delay = False
        self._reconnect_pending = False
        link = candidate = None
        self._connecting = True
        try:
            candidates = self.candidates()
            self._tried = {c.address for c in candidates}
            for candidate in candidates:
                link = await self._try(candidate)
                if link is not None:
                    break
        finally:
            self._connecting = False
        if link is None or candidate is None:
            await self._sleep(backoff)
            return min(backoff * 2, BACKOFF_MAX)
        started = loop.time()
        await self._serve(link, candidate)
        if self._skip_delay:
            self._skip_delay = False
            return BACKOFF_MIN
        if self._idle_timeout and not self._demand.is_set():
            return BACKOFF_MIN
        if loop.time() - started >= BACKOFF_MAX:
            backoff = BACKOFF_MIN  # a link that held for a while is not flapping
        await self._sleep(backoff)
        return min(backoff * 2, BACKOFF_MAX)

    async def _sleep(self, delay: float) -> None:
        """Sleep, unless an advert from a new proxy (or reconnect/demand) wakes us."""
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)
        self._wake.clear()

    async def _try(self, candidate: ProxyCandidate) -> ProxyLink | None:
        _LOGGER.debug("Connecting to mesh proxy %s (RSSI %s)", candidate.address, candidate.rssi)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                transport = await self._radio.connect(candidate.address)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # bleak, HA bluetooth and timeouts all have their own types
            _LOGGER.info("Mesh proxy %s not reachable: %s", candidate.address, err)
            return None
        self._generation += 1
        self._closed = asyncio.Event()
        link = ProxyLink(
            self._network,
            transport,
            dev_key_for=self._dev_key_for,
            on_message=self._on_message,
            on_beacon=self._on_beacon,
            on_close=partial(self._on_link_closed, self._generation),
        )
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await link.start(use_filter=self._use_filter)
        except asyncio.CancelledError:
            await self._close_link(link)
            raise
        except Exception as err:
            _LOGGER.info("Mesh proxy %s setup failed: %s", candidate.address, err)
            await self._close_link(link)
            return None
        if not link.is_connected:
            await self._close_link(link)
            return None
        if self._use_filter and not link.filter_ok:
            _LOGGER.info("Mesh proxy %s ignored the filter; using accept-all", candidate.address)
        return link

    async def _serve(self, link: ProxyLink, candidate: ProxyCandidate) -> None:
        self._link, self._proxy = link, candidate
        self.touch()
        self._connected.set()
        if self._reconnect_pending:
            self._reconnect_pending = False
            self._closed.set()
        _LOGGER.info("Connected to mesh proxy %s", candidate.address)
        try:
            try:
                await self._on_connected(link, candidate)
            except (LinkClosedError, MeshTimeout) as err:
                _LOGGER.debug("Startup on %s cut short: %s", candidate.address, err)
            except Exception:
                _LOGGER.exception("Mesh connect handler failed")
            await self._wait_closed_or_idle()
        finally:
            self._connected.clear()
            self._link = self._proxy = None
            _LOGGER.info("Disconnected from mesh proxy %s", candidate.address)
            try:
                self._on_disconnected()
            except Exception:
                _LOGGER.exception("Mesh disconnect handler failed")
            await self._close_link(link)  # also closes the bleak client after a remote drop

    async def _close_link(self, link: ProxyLink) -> None:
        """Await link.close(), never raising its errors; finish it even if we are cancelled."""
        task = asyncio.ensure_future(link.close())
        cancelled: asyncio.CancelledError | None = None
        try:
            await asyncio.wait({task}, timeout=CLOSE_TIMEOUT)
        except asyncio.CancelledError as err:
            cancelled = err
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task}, timeout=CLOSE_TIMEOUT)
        if not task.done():
            task.cancel()
            _LOGGER.warning("Closing the mesh link timed out")
        elif not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.debug("Closing the mesh link failed: %r", error)
        if cancelled is not None:
            raise cancelled

    async def _wait_closed_or_idle(self) -> None:
        closed = self._closed
        loop = asyncio.get_running_loop()
        while not closed.is_set():
            if self._idle_timeout <= 0:
                await closed.wait()
                return
            remaining = self._last_activity + self._idle_timeout - loop.time()
            if remaining <= 0:
                _LOGGER.debug("Mesh link idle for %ss: disconnecting", self._idle_timeout)
                self._demand.clear()
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(closed.wait(), remaining)

    def _on_link_closed(self, generation: int) -> None:
        if generation != self._generation:
            return  # a callback from an older link
        self._closed.set()

    def _on_advert(self, candidate: ProxyCandidate) -> None:
        # Only a proxy we did not try last time wakes the loop, so a storm of adverts from a
        # proxy that keeps failing cannot defeat the backoff.
        if self._link is not None or candidate.address in self._tried:
            return
        if self.matches(candidate):
            self._wake.set()
