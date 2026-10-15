"""MeshNetwork: keys, IV index and IV Update, seq reservation and the replay cache."""

from __future__ import annotations

import enum
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from .crypto import k4
from .errors import LinkClosedError, MeshError, SeqNotSaved
from .pdu import NetworkKeys, SecureBeacon

SEQ_BLOCK = 64
RESERVE_MARGIN = SEQ_BLOCK // 2  # reserve the next block this far before the ceiling
SEQ_MAX = 0xFFFFFF
IV_INDEX_MAX = 0xFFFFFFFF

_LOGGER = logging.getLogger(__name__)


class BeaconOutcome(enum.Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    TOO_FAR = "too_far"  # more than one IV step ahead: reported, never applied


class MeshNetwork:
    """Persistent state of one mesh network, as seen by our provisioner address.

    Contract: a seq is handed out only when a saved state covers it at the current
    transmit IV index, so a crash can never reuse an (IV index, seq) pair.

    ``on_persist`` is called with the state to save. With ``persist_is_durable`` True, a
    call that returns without raising counts as saved. With False (an async store), the
    owner calls ``mark_persisted`` with that exact state once the write has finished.
    """

    def __init__(
        self,
        *,
        net_key: bytes,
        app_key: bytes,
        address: int,
        net_key_index: int = 0,
        app_key_index: int = 0,
        state: dict[str, Any] | None = None,
        on_persist: Callable[[dict[str, Any]], None] | None = None,
        persist_is_durable: bool = True,
    ) -> None:
        if len(net_key) != 16 or len(app_key) != 16:
            raise ValueError("NetKey and AppKey must be 16 bytes")
        if not 0x0001 <= address <= 0x7FFF:
            raise ValueError(f"0x{address:04X} is not a unicast address")
        self.keys = NetworkKeys.from_net_key(net_key)
        self.app_key = app_key
        self.aid = k4(app_key)
        self.address = address
        self.net_key_index = net_key_index
        self.app_key_index = app_key_index
        state = state or {}
        self._iv_index = int(state.get("iv_index", 0))
        self._iv_update = bool(state.get("iv_update", False))
        self._seq = int(state.get("seq", 0))
        self._ceiling = self._seq
        self._replay: dict[int, tuple[int, int]] = {
            int(src): (int(last[0]), int(last[1])) for src, last in state.get("replay", {}).items()
        }
        if not 0 <= self._iv_index <= IV_INDEX_MAX:
            raise ValueError(f"stored IV index {self._iv_index} is out of range")
        if not 0 <= self._seq <= SEQ_MAX + 1:
            raise ValueError(f"stored seq {self._seq} is out of range")
        for src, (iv, seq) in self._replay.items():
            if not (0 <= iv <= IV_INDEX_MAX and 0 <= seq <= SEQ_MAX):
                raise ValueError(f"stored replay entry for 0x{src:04X} is out of range")
        self._replay_dirty = False
        self.replay_drops: Counter[int] = Counter()  # per src, since start
        self._on_persist = on_persist
        self._persist_is_durable = persist_is_durable
        # The highest saved (transmit IV index, seq ceiling). A restored state came from disk.
        self._durable_tx_iv = self.tx_iv_index  # (0, 0) without a state
        self._durable_ceiling = self._ceiling
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"MeshNetwork(address=0x{self.address:04X}, nid=0x{self.keys.nid:02X}, "
            f"iv_index={self._iv_index}, iv_update={self._iv_update})"
        )

    def persistent_state(self) -> dict[str, Any]:
        return {
            "iv_index": self._iv_index,
            "iv_update": self._iv_update,
            "seq": self._ceiling,
            "replay": {str(src): [iv, seq] for src, (iv, seq) in self._replay.items()},
        }

    def _persist(self) -> None:
        if self._on_persist is not None:
            self._on_persist(self.persistent_state())
        if self._persist_is_durable:
            self._mark_durable(self.tx_iv_index, self._ceiling)

    def _mark_durable(self, tx_iv_index: int, ceiling: int) -> None:
        """Advance the saved (transmit IV index, ceiling) pair; it never moves back."""
        if (tx_iv_index, ceiling) > (self._durable_tx_iv, self._durable_ceiling):
            self._durable_tx_iv = tx_iv_index
            self._durable_ceiling = ceiling

    def mark_persisted(self, state: dict[str, Any]) -> None:
        """Confirm that ``state`` (exactly as given to ``on_persist``) is now on disk.

        If a save fails, call prime() again; it re-persists the state.
        """
        tx_iv_index = _tx_iv_index(int(state["iv_index"]), bool(state["iv_update"]))
        self._mark_durable(tx_iv_index, int(state["seq"]))

    def _reserve(self, ceiling: int) -> None:
        """Raise the ceiling and persist it; a failed persist restores the old one."""
        previous = self._ceiling
        self._ceiling = ceiling
        try:
            self._persist()
        except Exception:
            self._ceiling = previous
            raise

    def prime(self) -> None:
        """Reserve and persist the next block now, before any seq is needed.

        With an async store, await the save and ``mark_persisted`` before starting a link,
        so the first message after a restart is not refused. If a save fails, call prime()
        again; it re-persists the state.
        """
        self._reserve(min(max(self._ceiling, self._seq + SEQ_BLOCK), SEQ_MAX + 1))

    def close(self) -> None:
        """Shut down: from now on next_seq raises LinkClosedError, so nothing is sent.

        The state can still be read (and saved) afterwards.
        """
        self._closed = True

    def next_seq(self, count: int = 1) -> int:
        """Hand out ``count`` consecutive seqs.

        The next block is reserved and persisted once the seqs come within RESERVE_MARGIN
        of the ceiling. Raises ``SeqNotSaved`` (taking no seq) when no saved state covers
        the seqs at the current transmit IV index yet: the save is pending, so try again.
        Raises ``LinkClosedError`` once the network is closed.
        """
        if self._closed:
            raise LinkClosedError("the mesh network is shut down")
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count}")
        first = self._seq
        end = first + count
        if end > SEQ_MAX + 1:
            raise MeshError("sequence numbers exhausted; the network needs an IV Update")
        if end > self._ceiling - RESERVE_MARGIN:
            self._reserve(min(max(self._ceiling, end) + SEQ_BLOCK, SEQ_MAX + 1))
        if self.tx_iv_index != self._durable_tx_iv or end > self._durable_ceiling:
            raise SeqNotSaved("Sequence numbers are reserved but not saved yet; try again shortly")
        self._seq = end
        return first

    def accept_replay(self, src: int, seq: int, iv_index: int) -> bool:
        """Replay protection: accept only a newer (iv_index, seq) than last seen from src."""
        last = self._replay.get(src)
        if last is not None and (iv_index, seq) <= last:
            self.replay_drops[src] += 1
            _LOGGER.debug(
                "Replay drop from 0x%04X: IV %d seq %d, last %s (%d so far); a node that "
                "restarts its seq after a power cycle shows up here",
                src,
                iv_index,
                seq,
                last,
                self.replay_drops[src],
            )
            return False
        self._replay[src] = (iv_index, seq)
        self._replay_dirty = True
        return True

    def forget_replay(self, first: int, count: int) -> None:
        """Clear the cache for a node's elements, e.g. after it is reset and re-provisioned."""
        for src in range(first, first + count):
            if self._replay.pop(src, None) is not None:
                self._replay_dirty = True

    def take_replay_dirty(self) -> bool:
        """True if the replay cache changed since the last call, which clears the flag.

        Replay updates are not persisted one by one: poll this on a debounced save timer
        and at shutdown, and save ``persistent_state()`` when it returns True.
        """
        dirty = self._replay_dirty
        self._replay_dirty = False
        return dirty

    @property
    def iv_index(self) -> int:
        return self._iv_index

    @property
    def iv_update(self) -> bool:
        return self._iv_update

    @property
    def tx_iv_index(self) -> int:
        """While an IV Update is in progress we keep sending with the old IV index."""
        return _tx_iv_index(self._iv_index, self._iv_update)

    def rx_iv_index(self, ivi: int) -> int | None:
        """The IV index a received PDU used, picked by its IVI bit."""
        if ivi == (self._iv_index & 1):
            return self._iv_index
        return self._iv_index - 1 if self._iv_index > 0 else None

    def _reset_seq(self) -> None:
        """Restart seq on a new transmit IV index, reserving the first block.

        The caller persists right after, so one confirmed save covers the first sends.
        """
        self._seq = 0
        self._ceiling = SEQ_BLOCK

    def apply_beacon(self, beacon: SecureBeacon) -> BeaconOutcome:
        """Apply a verified Secure Network Beacon (IV Update procedure).

        Invariant: the transmit IV index never decreases, and seq resets only when it
        increases, so an (IV index, seq) pair is never used twice.
        """
        if beacon.iv_index > self._iv_index + 1:
            return BeaconOutcome.TOO_FAR
        if beacon.iv_index < self._iv_index:
            return BeaconOutcome.UNCHANGED
        if beacon.iv_index == self._iv_index and not (self._iv_update and not beacon.iv_update):
            # Only In Progress -> Normal is allowed at the same IV index. Normal(N) ->
            # In Progress(N) would move the transmit IV index back to N-1.
            return BeaconOutcome.UNCHANGED
        old_tx = self.tx_iv_index
        self._iv_index = beacon.iv_index
        self._iv_update = beacon.iv_update
        new_tx = self.tx_iv_index
        if new_tx > old_tx:  # a transmit IV index we have never used starts from seq 0
            self._reset_seq()
        self._persist()
        return BeaconOutcome.UPDATED

    def accept_iv_jump(self, beacon: SecureBeacon) -> None:
        """Adopt a verified beacon's IV index after the user confirmed a TOO_FAR jump.

        Raises ``ValueError`` unless the beacon's transmit IV index is higher than ours:
        seq restarts from zero, which is safe only on a transmit IV index we have never
        used. (IV N+1 In Progress still transmits with N, so it is refused from Normal N.)
        The block is reserved here, so with an async store the caller awaits the save (and
        ``mark_persisted``) before sending.
        """
        if not 0 <= beacon.iv_index <= IV_INDEX_MAX:
            raise ValueError(f"IV index {beacon.iv_index} is outside 0..{IV_INDEX_MAX}")
        new_tx = _tx_iv_index(beacon.iv_index, beacon.iv_update)
        if new_tx <= self.tx_iv_index:
            raise ValueError(
                f"transmit IV index {new_tx} is not ahead of ours ({self.tx_iv_index})"
            )
        self._iv_index = beacon.iv_index
        self._iv_update = beacon.iv_update
        self._reset_seq()
        self._persist()


def _tx_iv_index(iv_index: int, iv_update: bool) -> int:
    """The IV index we send with: the old one while an IV Update is in progress."""
    if iv_update and iv_index > 0:
        return iv_index - 1
    return iv_index
