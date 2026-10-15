"""One coordinator per mesh network: the proxy connection, node states and persistence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADDRESS,
    CONF_APP_KEY,
    CONF_APP_KEY_INDEX,
    CONF_CONFIGURED,
    CONF_IDLE_TIMEOUT,
    CONF_IV_INDEX,
    CONF_MAC,
    CONF_NET_KEY,
    CONF_NET_KEY_INDEX,
    CONF_PUBLISH_SETUP,
    CONF_ROLE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_PUBLISH_SETUP,
    DOMAIN,
    ISSUE_IV_INDEX_JUMP,
    ISSUE_KEYS_REJECTED,
    ISSUE_NO_PROXY,
    ISSUE_NODE_UNREADABLE,
    ROLE_LIGHT,
    ROLE_SWITCH,
    SUBENTRY_NODE,
)
from .mesh.configure import ConfigureError, configure_node
from .mesh.connection import ConnectionManager, MeshRadio, ProxyCandidate
from .mesh.control import NodeCommands, NodeState, TidCounter, apply_status
from .mesh.errors import LinkClosedError, MeshError, MeshTimeout, SeqNotSaved
from .mesh.network import BeaconOutcome, MeshNetwork
from .mesh.node import ROLE_HEALTH, ROLE_LIGHTNESS, MeshNode
from .mesh.pdu import SecureBeacon
from .mesh.proxy import AccessMessage, ProxyLink
from .mesh.quirks import Quirk, is_vendor_opcode, quirks_for
from .store import NetworkStore, StateSaver

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Read at call time, so tests can shorten them with monkeypatch.
POLL_INTERVAL = timedelta(minutes=5)
RSSI_INTERVAL = timedelta(seconds=60)
REPLAY_SAVE_INTERVAL = timedelta(seconds=30)  # replay cache changes are saved in batches
SAVE_RETRY_MIN = 1.0  # seconds; a failed save is retried, doubling up to SAVE_RETRY_MAX
SAVE_RETRY_MAX = 60.0
LINK_DOWN_GRACE = 30.0  # seconds without a link before nodes go unavailable
NO_PROXY_ISSUE_AFTER = 600.0  # seconds without a link before the repair issue
FAILURES_UNAVAILABLE = 3  # unanswered requests in a row
REFRESH_STAGGER = 0.3  # seconds between nodes when reading every node's state
COMMAND_CONNECT_TIMEOUT = 15.0
SAVE_WAIT = 10.0  # seconds a command waits for its seq block's save before giving up
CONFIGURE_TIMEOUT = 90.0
FOLLOW_UP_MARGIN = 0.5  # seconds after a transition ends before reading the final state


# What MeshNode.from_dict raises for a damaged subentry (OverflowError: int() of an infinity).
NODE_DATA_ERRORS = (KeyError, TypeError, ValueError, AttributeError, OverflowError)


ROLES = (ROLE_LIGHT, ROLE_SWITCH)


def read_node(data: Any) -> MeshNode | None:
    """The node in subentry data, or None when the data is damaged.

    The MAC and role are checked here too, so a bad value skips the node instead of failing
    the whole network.
    """
    try:
        data = dict(data)
        mac, role = data.get(CONF_MAC), data.get(CONF_ROLE)
        if mac is not None and not isinstance(mac, str):
            return None
        if role is not None and role not in ROLES:
            return None
        return MeshNode.from_dict(data)
    except NODE_DATA_ERRORS:
        return None


def claimed_range(data: Any) -> tuple[int, int] | None:
    """(first address, count) a subentry claims, read as far as damaged data allows."""
    try:
        unicast = int(data["unicast"])
    except NODE_DATA_ERRORS:
        return None
    try:
        count = max(1, int(data.get("element_count", 1)))
    except NODE_DATA_ERRORS:
        count = 1
    return unicast, count


def default_role(node: MeshNode) -> str:
    return ROLE_LIGHT if node.is_light else ROLE_SWITCH


def node_role(data: Mapping[str, Any], node: MeshNode | None) -> str:
    """The stored role if it is one we know (an explicit None is missing), else what the node
    is; light when the node is unreadable."""
    role = data.get(CONF_ROLE)
    if isinstance(role, str) and role in ROLES:
        return role
    return default_role(node) if node is not None else ROLE_LIGHT


def capabilities(node: MeshNode) -> tuple[Any, ...]:
    """What decides which entities a node gets; a change needs a reload."""
    return (
        node.is_light,
        node.is_tunable,
        ROLE_LIGHTNESS in node.models,
        node.kelvin_min,
        node.kelvin_max,
        ROLE_HEALTH in node.models and not node.is_light,
    )


@dataclass
class NodeRecord:
    """A node subentry plus what we know about it at runtime."""

    subentry_id: str
    title: str
    node: MeshNode
    role: str
    mac: str | None
    configured: bool
    quirks: Quirk
    state: NodeState = field(default_factory=NodeState)  # confirmed plus optimistic
    confirmed: NodeState = field(default_factory=NodeState)  # only what statuses said
    generation: int = 0  # bumped by every status applied and every command started
    command_generation: int = 0  # the generation the newest command took
    pending: int = 0  # commands begun and not finished yet
    available: bool = True
    failures: int = 0
    last_seen: datetime | None = None


class MeshCoordinator:
    """Owns the MeshNetwork, the ConnectionManager and every node's state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        radio: MeshRadio,
        store: NetworkStore,
        state: dict[str, Any] | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.radio = radio
        self.store = store
        self._saver = StateSaver(hass, store, self._on_saved, self._on_save_failed)
        self._save_retry: CALLBACK_TYPE | None = None
        self._save_backoff = SAVE_RETRY_MIN
        self._stopping = False
        self._stopped = asyncio.Event()  # set with _stopping: commands stop waiting for a link
        self._shutdown: asyncio.Task[None] | None = None
        self._save_failed = False  # a save failed since the last one that landed
        self._refreshing = 0  # running _async_refresh_all passes
        self._pass_lock = asyncio.Lock()  # one setup-and-read pass at a time
        data = entry.data
        if state is None:
            state = {
                "iv_index": int(data.get(CONF_IV_INDEX, 0)),
                "iv_update": False,
                "seq": 0,
                "replay": {},
            }
        self.network = MeshNetwork(
            net_key=bytes.fromhex(data[CONF_NET_KEY]),
            app_key=bytes.fromhex(data[CONF_APP_KEY]),
            address=int(data[CONF_ADDRESS]),
            net_key_index=int(data.get(CONF_NET_KEY_INDEX, 0)),
            app_key_index=int(data.get(CONF_APP_KEY_INDEX, 0)),
            state=state,
            on_persist=self._saver.save,
            persist_is_durable=False,
        )
        self.network_id = self.network.keys.network_id.hex()
        self.nodes: dict[str, NodeRecord] = {}
        unreadable: list[str] = []
        claimed: list[tuple[int, int]] = []
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_NODE:
                continue
            if (addresses := claimed_range(subentry.data)) is not None:
                claimed.append(addresses)
            node = read_node(subentry.data)
            if node is None:
                _LOGGER.error("%s: the saved device data is damaged; skipped", subentry.title)
                unreadable.append(subentry.title)
                continue
            self.nodes[subentry_id] = NodeRecord(
                subentry_id=subentry_id,
                title=subentry.title,
                node=node,
                role=node_role(subentry.data, node),
                mac=(subentry.data.get(CONF_MAC) or "").upper() or None,
                configured=bool(subentry.data.get(CONF_CONFIGURED, False)),
                quirks=quirks_for(node.cid, node.pid),
            )
        self._report_unreadable(unreadable)
        self._loaded = self._snapshot(entry)
        self._prune_replay(claimed)
        idle_minutes = float(entry.options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
        self.manager = ConnectionManager(
            self.network,
            radio,
            dev_key_for=self._dev_key_for,
            node_addresses=self._node_addresses,
            on_connected=self._async_on_connected,
            on_disconnected=self._on_disconnected,
            on_message=self._on_message,
            on_beacon=self._on_beacon,
            idle_timeout=idle_minutes * 60,
        )
        self._tids = TidCounter()
        self._listeners: dict[str | None, list[CALLBACK_TYPE]] = {}
        self._follow_ups: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._link_down_timer: CALLBACK_TYPE | None = None
        self._no_proxy_timer: CALLBACK_TYPE | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heard = False
        self.rssi: int | None = None
        self.rssi_seen: float | None = None  # time.monotonic() of the advert
        self.proxy_address: str | None = None
        self.pending_iv_jump: SecureBeacon | None = None  # a TOO_FAR beacon awaiting the user

    # lookups

    @staticmethod
    def _snapshot(entry: ConfigEntry) -> tuple[dict[str, Any], dict[str, tuple[Any, ...]]]:
        """What entities and connection settings are built from: options, roles, capabilities."""
        nodes: dict[str, tuple[Any, ...]] = {}
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_NODE:
                continue
            node = read_node(subentry.data)
            nodes[subentry_id] = (
                (node_role(subentry.data, node), *capabilities(node)) if node is not None else ()
            )
        return dict(entry.options), nodes

    def needs_reload(self) -> bool:
        """The entry changed in a way only a reload applies (nodes, roles, capabilities, options).

        Updates that only record setup progress (``configured``, a refined element count)
        leave the snapshot alone and keep the connection up.
        """
        return self._snapshot(self.entry) != self._loaded

    def _report_unreadable(self, titles: list[str]) -> None:
        issue_id = f"{ISSUE_NODE_UNREADABLE}_{self.entry.entry_id}"
        if not titles:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_NODE_UNREADABLE,
            translation_placeholders={"network": self.entry.title, "names": ", ".join(titles)},
        )

    def _prune_replay(self, claimed: list[tuple[int, int]]) -> None:
        """Forget replay entries of addresses no subentry claims (removed, maybe re-provisioned).

        A damaged subentry still claims its addresses: the node is still on the mesh. The
        change is saved by ``async_prime``, which writes the whole state.
        """
        for src in list(self.network.persistent_state()["replay"]):
            address = int(src)
            if not any(first <= address < first + count for first, count in claimed):
                self.network.forget_replay(address, 1)

    def _dev_key_for(self, address: int) -> bytes | None:
        record = self.record_for(address)
        return record.node.dev_key if record is not None else None

    def _node_addresses(self) -> set[str]:
        return {record.mac for record in self.nodes.values() if record.mac}

    def record_for(self, address: int) -> NodeRecord | None:
        for record in self.nodes.values():
            if record.node.contains(address):
                return record
        return None

    @property
    def connected(self) -> bool:
        return self.manager.link is not None

    @property
    def connected_via(self) -> str | None:
        """The connected proxy: a node's name when it is one of ours, else its name or MAC."""
        proxy = self.manager.proxy
        if proxy is None:
            return None
        for record in self.nodes.values():
            if record.mac == proxy.address:
                return record.title
        return proxy.name or proxy.address

    @property
    def rssi_age(self) -> float | None:
        """Seconds since the proxy's advert behind ``rssi``."""
        if self.rssi_seen is None:
            return None
        return max(0.0, time.monotonic() - self.rssi_seen)

    # listeners

    @callback
    def async_add_listener(self, key: str | None, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update`` when node ``key`` (a subentry id) or the network (None) changes."""
        self._listeners.setdefault(key, []).append(update)

        @callback
        def _remove() -> None:
            self._listeners[key].remove(update)

        return _remove

    @callback
    def _notify(self, key: str | None) -> None:
        for update in list(self._listeners.get(key, ())):
            update()

    @callback
    def _notify_all(self) -> None:
        for key in list(self._listeners):
            self._notify(key)

    # persistence

    @callback
    def _on_saved(self, state: dict[str, Any]) -> None:
        self.network.mark_persisted(state)
        self._save_backoff = SAVE_RETRY_MIN
        if not self._save_failed:
            return
        self._save_failed = False
        # Setup and reads were skipped (SeqNotSaved) while the saves failed: catch up now
        # rather than at the next drop. Without a link, the next connect does it anyway.
        if not self._stopping and (link := self.manager.link) is not None:
            self._spawn(self._async_connect_pass(link), "aoi_sig_mesh catch-up")

    @callback
    def _on_save_failed(self) -> None:
        """Without a retry ``next_seq`` never reserves again and every send is refused."""
        self._save_failed = True
        if self._stopping or self._save_retry is not None:
            return
        delay = self._save_backoff
        self._save_backoff = min(delay * 2, SAVE_RETRY_MAX)
        _LOGGER.warning("Retrying the mesh state save in %.0f s", delay)
        self._save_retry = async_call_later(self.hass, delay, self._retry_save)

    @callback
    def _retry_save(self, _now: datetime) -> None:
        self._save_retry = None
        self.network.prime()  # reserves again (or re-sends the current ceiling) and saves

    async def async_prime(self) -> None:
        """Reserve the next seq block and wait until it is on disk. Call before ``start``."""
        self.network.prime()
        await self._saver.async_flush()

    async def _async_save_state(self) -> None:
        """Write the whole state now and wait for it (and for any write still queued)."""
        self._saver.save(self.network.persistent_state())
        await self._saver.async_flush()

    @callback
    def _replay_tick(self, _now: datetime) -> None:
        if self.network.take_replay_dirty():
            self._saver.save(self.network.persistent_state())

    @callback
    def async_forget_node(self, unicast: int, element_count: int) -> None:
        """A node was reset or provisioned again: accept its restarted seq from now on."""
        if (link := self.manager.link) is not None:
            link.forget_node(unicast, element_count)
        else:
            self.network.forget_replay(unicast, element_count)

    # lifecycle

    @callback
    def start(self) -> None:
        self._link_down()
        self.manager.start()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._replay_tick, REPLAY_SAVE_INTERVAL)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._async_poll, POLL_INTERVAL))
        self._unsubs.append(async_track_time_interval(self.hass, self._rssi_tick, RSSI_INTERVAL))
        self._unsubs.append(self.radio.subscribe(self._on_advert))

    async def async_shutdown(self) -> None:
        """Stop everything and write the mesh state (seq ceiling, IV, replay cache).

        A second call (HA stopping while the entry unloads) waits for the same shutdown.
        """
        if self._shutdown is None:
            self._stopping = True
            self._stopped.set()
            self._shutdown = self.hass.async_create_task(
                self._async_stop(), "aoi_sig_mesh shutdown"
            )
        await asyncio.shield(self._shutdown)

    async def _async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._follow_ups.values():
            cancel()
        self._follow_ups.clear()
        self._cancel_link_timers()
        if self._save_retry is not None:
            self._save_retry()
            self._save_retry = None
        await self.manager.stop()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.network.take_replay_dirty()  # the full state below includes the replay cache
        await self._async_save_state()

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task = self.entry.async_create_background_task(self.hass, coro, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # connection events

    async def _async_on_connected(self, link: ProxyLink, proxy: ProxyCandidate) -> None:
        self._cancel_link_timers()
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_NO_PROXY}_{self.entry.entry_id}")
        self.proxy_address = proxy.address
        self.rssi, self.rssi_seen = proxy.rssi, proxy.seen
        self._update_rssi()
        self._notify(None)
        await self._async_connect_pass(link)
        self._check_keys()

    async def _async_connect_pass(self, link: ProxyLink) -> None:
        """Set up the nodes that are not set up yet, then read every node's state.

        One pass at a time, so a catch-up after a failed save never sets up a node twice.
        """
        async with self._pass_lock:
            for record in list(self.nodes.values()):
                if not record.configured and link.is_connected:
                    await self._async_configure(link, record)
            await self._async_refresh_all(link)

    @callback
    def _on_disconnected(self) -> None:
        # "Heard since connecting": reset here, not in _async_on_connected, because the
        # proxy's first beacon arrives while the link is still starting (before that runs).
        self._heard = False
        self._notify(None)
        if not self._stopping:
            self._link_down()

    @callback
    def _link_down(self) -> None:
        """Start the grace timer (nodes unavailable) and the no-proxy repair timer."""
        self._cancel_link_timers()
        self._link_down_timer = async_call_later(
            self.hass, LINK_DOWN_GRACE, self._link_down_expired
        )
        self._no_proxy_timer = async_call_later(
            self.hass, NO_PROXY_ISSUE_AFTER, self._no_proxy_expired
        )

    @callback
    def _cancel_link_timers(self) -> None:
        for timer in (self._link_down_timer, self._no_proxy_timer):
            if timer is not None:
                timer()
        self._link_down_timer = self._no_proxy_timer = None

    @callback
    def _link_down_expired(self, _now: datetime) -> None:
        self._link_down_timer = None
        if self.connected or self.manager.idle:
            return
        for record in self.nodes.values():
            record.available = False
        self._notify_all()

    @callback
    def _no_proxy_expired(self, _now: datetime) -> None:
        self._no_proxy_timer = None
        if self.connected or self.manager.idle:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_NO_PROXY}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_PROXY,
            translation_placeholders={
                "network": self.entry.title,
                "minutes": str(round(NO_PROXY_ISSUE_AFTER / 60)),
            },
        )

    def _check_keys(self) -> None:
        """Nothing decrypted since connecting (no status, no beacon): the keys are wrong."""
        if self._heard:
            return  # _on_heard already deleted the issue
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_KEYS_REJECTED}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_KEYS_REJECTED,
            translation_placeholders={"network": self.entry.title},
        )

    # mesh traffic

    @callback
    def _on_heard(self) -> None:
        """Something decrypted: the keys are right."""
        if not self._heard:
            self._heard = True
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_KEYS_REJECTED}_{self.entry.entry_id}")

    @callback
    def _on_message(self, message: AccessMessage) -> None:
        self._on_heard()
        record = self.record_for(message.src)
        if record is None:
            return
        if is_vendor_opcode(message.opcode):
            if Quirk.IGNORE_VENDOR_OPCODES not in record.quirks:
                _LOGGER.debug("Vendor opcode 0x%06X from %s", message.opcode, record.title)
            return
        record.last_seen = dt_util.utcnow()
        record.failures = 0
        record.available = True
        update = apply_status(record.state, message)
        if update is not None:
            apply_status(record.confirmed, message)
            record.generation += 1  # a failed command no longer reverts over this
        self._notify(record.subentry_id)
        if update is not None and update.remaining_ms:
            self._schedule_follow_up(record, update.remaining_ms / 1000 + FOLLOW_UP_MARGIN)

    @callback
    def _on_beacon(self, beacon: SecureBeacon, outcome: BeaconOutcome) -> None:
        self._on_heard()
        issue_id = f"{ISSUE_IV_INDEX_JUMP}_{self.entry.entry_id}"
        if outcome is BeaconOutcome.TOO_FAR:
            # Proxies repeat their beacon every few seconds: warn once per IV index.
            pending = self.pending_iv_jump
            repeat = pending is not None and pending.iv_index == beacon.iv_index
            (_LOGGER.debug if repeat else _LOGGER.warning)(
                "%s: beacon IV index %d is more than one step past ours (%d); not applied",
                self.entry.title,
                beacon.iv_index,
                self.network.iv_index,
            )
            self.pending_iv_jump = beacon
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                data={"entry_id": self.entry.entry_id},
                translation_key=ISSUE_IV_INDEX_JUMP,
                translation_placeholders={
                    "network": self.entry.title,
                    "ours": str(self.network.iv_index),
                    "theirs": str(beacon.iv_index),
                },
            )
        elif outcome is BeaconOutcome.UPDATED:
            _LOGGER.info("%s: IV index now %d", self.entry.title, self.network.iv_index)
            self.pending_iv_jump = None
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_accept_iv_jump(self, beacon: SecureBeacon) -> None:
        """The user confirmed ``beacon`` (repair flow): adopt it and save it.

        Raises ``ValueError`` unless ``beacon`` is still the pending jump: the user agreed to
        that IV index, not to whichever one a proxy announced since.
        """
        if beacon != self.pending_iv_jump:
            raise ValueError("that IV index jump is no longer pending")
        old = self.network.iv_index
        self.network.accept_iv_jump(beacon)
        self.pending_iv_jump = None
        # Sends stay refused (SeqNotSaved) until the new block is on disk; a failed write is
        # retried by us (_on_save_failed), so nothing goes out before it lands.
        await self._saver.async_flush()
        if self.pending_iv_jump is None:  # a newer jump heard during the write keeps its issue
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_IV_INDEX_JUMP}_{self.entry.entry_id}")
        _LOGGER.warning(
            "%s: IV index jumped from %d to %d on request", self.entry.title, old, beacon.iv_index
        )

    @callback
    def _schedule_follow_up(self, record: NodeRecord, delay: float) -> None:
        if (cancel := self._follow_ups.pop(record.subentry_id, None)) is not None:
            cancel()

        @callback
        def _due(_now: datetime) -> None:
            self._follow_ups.pop(record.subentry_id, None)
            if (link := self.manager.link) is not None:
                self._spawn(self._async_refresh(link, record), "aoi_sig_mesh follow-up")

        self._follow_ups[record.subentry_id] = async_call_later(self.hass, delay, _due)

    # configuration and polling

    async def _async_configure(self, link: ProxyLink, record: NodeRecord) -> None:
        node = replace(record.node, models=dict(record.node.models))
        before = capabilities(node)
        publish = bool(self.entry.options.get(CONF_PUBLISH_SETUP, DEFAULT_PUBLISH_SETUP))
        try:
            async with asyncio.timeout(CONFIGURE_TIMEOUT):
                result = await configure_node(link, self.network, node, publish=publish)
        except (ConfigureError, LinkClosedError, TimeoutError, SeqNotSaved) as err:
            _LOGGER.warning(
                "%s: setup on the mesh failed, retrying next connect: %s", record.title, err
            )
            return
        finally:
            self.manager.touch()  # a long setup must not count as idle time
        _LOGGER.info(
            "%s: bound %s, publishing %s, %d-%d K",
            record.title,
            ", ".join(result.bound) or "nothing",
            ", ".join(result.published) or "nothing",
            node.kelvin_min,
            node.kelvin_max,
        )
        was_light = record.node.is_light
        record.node = node
        record.configured = True
        record.quirks = quirks_for(node.cid, node.pid)
        subentry = self.entry.subentries[record.subentry_id]
        data = {**subentry.data, **node.to_dict(), CONF_CONFIGURED: True}
        if capabilities(node) != before:
            _LOGGER.info("%s: Composition changed its capabilities", record.title)
        if node.is_light != was_light:
            # only a change of what the node is re-derives the role; a chosen one stays
            record.role = default_role(node)
            data[CONF_ROLE] = record.role
        self.hass.config_entries.async_update_subentry(self.entry, subentry, data=data)
        self._update_device_info(record)

    @callback
    def _update_device_info(self, record: NodeRecord) -> None:
        """Composition supplies manufacturer, model and version without a reload."""
        registry = dr.async_get(self.hass)
        identifier = (DOMAIN, f"{self.network_id}_{record.node.unicast:04x}")
        if (device := registry.async_get_device(identifiers={identifier})) is None:
            return
        from .entity import node_device_info  # entity imports this module

        info = node_device_info(self, record)
        registry.async_update_device(
            device.id,
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            sw_version=info.get("sw_version"),
        )

    async def _async_refresh_all(self, link: ProxyLink) -> None:
        self._refreshing += 1
        try:
            for index, record in enumerate(list(self.nodes.values())):
                if index:
                    await asyncio.sleep(REFRESH_STAGGER)
                if not link.is_connected:
                    return
                await self._async_refresh(link, record)
        finally:
            self._refreshing -= 1

    async def _async_refresh(self, link: ProxyLink, record: NodeRecord) -> None:
        try:
            await NodeCommands(link, record.node, record.quirks, self._tids).refresh()
        except (LinkClosedError, SeqNotSaved) as err:
            _LOGGER.debug("%s: refresh skipped: %s", record.title, err)
        except MeshTimeout:
            self._failed(record)
        finally:
            self.manager.touch()  # only get_link() counts as activity: a long read is too

    async def _async_poll(self, _now: datetime) -> None:
        if self._refreshing:
            _LOGGER.debug("%s: the last round of reads is still running", self.entry.title)
            return
        if (link := self.manager.link) is not None:
            self._spawn(self._async_refresh_all(link), "aoi_sig_mesh poll")

    @callback
    def _failed(self, record: NodeRecord) -> None:
        record.failures += 1
        if record.failures >= FAILURES_UNAVAILABLE and record.available:
            _LOGGER.info("%s: %d requests unanswered; unavailable", record.title, record.failures)
            record.available = False
            self._notify(record.subentry_id)

    # signal strength

    @callback
    def _on_advert(self, candidate: ProxyCandidate) -> None:
        if candidate.address == self.proxy_address and candidate.rssi is not None:
            self.rssi, self.rssi_seen = candidate.rssi, candidate.seen
            self._notify(None)

    @callback
    def _update_rssi(self) -> None:
        """HA's newest advert from the proxy; keep the last value when it has none."""
        if self.proxy_address is None:
            return
        seen = self.radio.last_seen(self.proxy_address)
        if (
            seen is not None
            and seen.rssi is not None
            and (self.rssi_seen is None or seen.seen >= self.rssi_seen)
        ):
            self.rssi, self.rssi_seen = seen.rssi, seen.seen

    @callback
    def _rssi_tick(self, _now: datetime) -> None:
        self._update_rssi()
        self._notify(None)

    # commands (called by entities)

    async def _async_command(
        self,
        record: NodeRecord,
        action: Callable[[NodeCommands], Awaitable[None]],
        before: NodeState | None = None,
        generation: int = 0,
    ) -> None:
        """Run ``action``; on failure revert to ``before`` and raise a translated error.

        ``before`` and ``generation`` come from ``_begin``; without them nothing is reverted.
        """
        try:
            if (link := await self._async_command_link()) is None:
                raise LinkClosedError("no mesh proxy connection")
            commands = NodeCommands(link, record.node, record.quirks, self._tids)
            try:
                await action(commands)
            except SeqNotSaved:  # the seq block's save is pending (or failed): save, retry once
                try:
                    async with asyncio.timeout(SAVE_WAIT):
                        await self._async_save_state()
                except TimeoutError as err:
                    raise SeqNotSaved("the mesh state save did not finish in time") from err
                await action(commands)
        except SeqNotSaved as err:  # the save still is not on disk
            self._revert(record, before, generation)
            raise self._error(record, "seq_not_saved") from err
        except MeshTimeout as err:  # before MeshError: an unanswered command counts as a miss
            self._revert(record, before, generation)
            self._failed(record)
            raise self._error(record, "command_failed") from err
        except LinkClosedError as err:  # no link, or it dropped: not the node's fault
            self._revert(record, before, generation)
            raise self._error(record, "not_connected") from err
        except MeshError as err:  # e.g. the TIDs ran out
            self._revert(record, before, generation)
            raise self._error(record, "command_failed") from err
        except BaseException:  # cancelled
            self._revert(record, before, generation)
            raise
        finally:
            if before is not None:  # begun with _begin
                record.pending -= 1
        if before is not None and record.pending == 0:
            self._settle(record)
        self.manager.touch()

    async def _async_command_link(self) -> ProxyLink | None:
        """The link, waiting for a connect; None if none comes up in time or we are stopping."""
        if self._stopping:
            return None
        if (link := self.manager.link) is not None:
            self.manager.touch()
            return link
        getting = asyncio.create_task(self.manager.get_link(COMMAND_CONNECT_TIMEOUT))
        stopping = asyncio.create_task(self._stopped.wait())
        try:
            await asyncio.wait((getting, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            getting.cancel()  # no effect once it is done
        if not getting.done() or getting.cancelled():
            return None
        try:
            return getting.result()
        except MeshTimeout:
            return None

    def _error(self, record: NodeRecord, key: str) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"name": record.title},
        )

    @callback
    def _begin(self, record: NodeRecord) -> int:
        """A command starts (after its optimistic state): returns its generation."""
        record.generation += 1
        record.command_generation = record.generation
        record.pending += 1
        self._notify(record.subentry_id)
        return record.generation

    @callback
    def _settle(self, record: NodeRecord) -> None:
        """The last pending command succeeded: show what the node said.

        Every optimistic command is acknowledged, so its status is already in ``confirmed``;
        what is left in ``state`` is an older failed command's optimism (a status may carry
        only some fields, e.g. the temperature but not the brightness).
        """
        if record.state != record.confirmed:
            record.state = replace(record.confirmed)
            self._notify(record.subentry_id)

    @callback
    def _revert(self, record: NodeRecord, before: NodeState | None, generation: int) -> None:
        """Undo a failed command's optimistic state, but never over anything newer.

        No other command is pending: settle on what the node said (``before`` may still hold
        an older command's optimism, and that command has failed too). Otherwise: nothing
        happened since the command started, so restore ``before``; only statuses came in, so
        drop the optimistic fields still on top of them; or a newer command started, so its
        optimistic state stays.
        """
        if before is None:
            return
        if record.pending == 1:  # only this command (it is counted until it returns)
            record.state = replace(record.confirmed)
        elif record.generation == generation:
            record.state = before
        elif record.command_generation == generation:
            record.state = replace(record.confirmed)
        else:
            return
        self._notify(record.subentry_id)

    async def async_turn_on(
        self,
        subentry_id: str,
        *,
        lightness: int | None = None,
        kelvin: int | None = None,
        transition_ms: int | None = None,
    ) -> None:
        record = self.nodes[subentry_id]
        if record.role == ROLE_SWITCH:  # a switch sends Generic OnOff only
            lightness = kelvin = None
        before = replace(record.state)
        currently_on = bool(record.confirmed.on)  # a pending command's optimism is no proof
        record.state.on = True
        if lightness is not None:
            record.state.lightness = lightness
        if kelvin is not None and record.node.is_tunable:
            record.state.kelvin = max(record.node.kelvin_min, min(record.node.kelvin_max, kelvin))
        generation = self._begin(record)
        await self._async_command(
            record,
            lambda commands: commands.turn_on(
                currently_on=currently_on,
                lightness=lightness,
                kelvin=kelvin,
                transition_ms=transition_ms,
            ),
            before,
            generation,
        )

    async def async_turn_off(self, subentry_id: str, *, transition_ms: int | None = None) -> None:
        record = self.nodes[subentry_id]
        onoff_only = record.role == ROLE_SWITCH
        before = replace(record.state)
        record.state.on = False
        generation = self._begin(record)
        await self._async_command(
            record,
            lambda commands: commands.turn_off(transition_ms=transition_ms, onoff_only=onoff_only),
            before,
            generation,
        )

    async def async_identify(self, subentry_id: str) -> None:
        record = self.nodes[subentry_id]
        currently_on = bool(record.confirmed.on)  # the blinks end where the node really is
        await self._async_command(
            record, lambda commands: commands.identify(currently_on=currently_on)
        )

    @callback
    def async_reconnect(self) -> None:
        self.manager.reconnect()
