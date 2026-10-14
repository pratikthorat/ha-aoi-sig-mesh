"""MeshNetwork: keys, IV index and IV Update, seq reservation and the replay cache."""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any

from .crypto import k4
from .errors import MeshError
from .pdu import NetworkKeys, SecureBeacon

SEQ_BLOCK = 64
SEQ_MAX = 0xFFFFFF


class BeaconOutcome(enum.Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    TOO_FAR = "too_far"  # more than one IV step ahead: reported, never applied


class MeshNetwork:
    """Persistent state of one mesh network, as seen by our provisioner address."""

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
        self._on_persist = on_persist

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

    def next_seq(self, count: int = 1) -> int:
        """Hand out ``count`` consecutive seqs; the reserved ceiling is persisted first."""
        first = self._seq
        end = first + count
        if end > SEQ_MAX + 1:
            raise MeshError("sequence numbers exhausted; the network needs an IV Update")
        if end > self._ceiling:
            previous = self._ceiling
            self._ceiling = min(end + SEQ_BLOCK, SEQ_MAX + 1)
            try:
                self._persist()
            except Exception:
                self._ceiling = previous
                raise
        self._seq = end
        return first

    def accept_replay(self, src: int, seq: int, iv_index: int) -> bool:
        """Replay protection: accept only a newer (iv_index, seq) than last seen from src."""
        last = self._replay.get(src)
        if last is not None and (iv_index, seq) <= last:
            return False
        self._replay[src] = (iv_index, seq)
        return True

    def forget_replay(self, first: int, count: int) -> None:
        """Clear the cache for a node's elements, e.g. after it is reset and re-provisioned."""
        for src in range(first, first + count):
            self._replay.pop(src, None)

    @property
    def iv_index(self) -> int:
        return self._iv_index

    @property
    def iv_update(self) -> bool:
        return self._iv_update

    @property
    def tx_iv_index(self) -> int:
        """While an IV Update is in progress we keep sending with the old IV index."""
        if self._iv_update and self._iv_index > 0:
            return self._iv_index - 1
        return self._iv_index

    def rx_iv_index(self, ivi: int) -> int | None:
        """The IV index a received PDU used, picked by its IVI bit."""
        if ivi == (self._iv_index & 1):
            return self._iv_index
        return self._iv_index - 1 if self._iv_index > 0 else None

    def _reset_seq(self) -> None:
        self._seq = 0
        self._ceiling = 0

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
