"""MeshNetwork: keys, IV index and IV Update, seq reservation and the replay cache."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .crypto import k4
from .errors import MeshError
from .pdu import NetworkKeys

SEQ_BLOCK = 64
SEQ_MAX = 0xFFFFFF


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
            "replay": {},
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
