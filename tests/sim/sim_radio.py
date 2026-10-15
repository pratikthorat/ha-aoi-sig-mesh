"""A fake Home Assistant Bluetooth radio for SimMesh (implements mesh.connection.MeshRadio)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from mesh.connection import ProxyCandidate
from mesh.errors import MeshError
from mesh.pdu import NetworkKeys
from mesh.proxy import node_identity_data
from tests.sim.mesh_sim import LoopbackTransport, SimMesh

BULB_MAC = "DC:23:50:00:00:19"


class SimRadio:
    def __init__(self, mesh: SimMesh) -> None:
        self.mesh = mesh
        self.adverts: dict[str, ProxyCandidate] = {}
        self.connects: list[str] = []
        self.transports: list[LoopbackTransport] = []
        self.fail_connects = 0
        self.unreachable: set[str] = set()
        self._callbacks: list[Callable[[ProxyCandidate], None]] = []

    def advertise(
        self,
        address: str = BULB_MAC,
        *,
        rssi: int | None = -60,
        network_id: bytes | None = None,
        identity: bool = False,
        unicast: int | None = None,
        net_key: bytes | None = None,
        name: str | None = None,
    ) -> ProxyCandidate:
        """One 0x1828 advert: Network ID (ours unless given) or a Node Identity.

        A Node Identity advert carries the real hash for ``unicast`` (under ``net_key``, ours
        unless given); without a unicast its hash matches no node.
        """
        if identity and unicast is not None:
            keys = self.mesh.keys if net_key is None else NetworkKeys.from_net_key(net_key)
            data = node_identity_data(keys.identity_key, os.urandom(8), unicast)
        elif identity:
            data = bytes([0x01]) + bytes(16)
        else:
            data = bytes([0x00]) + (network_id or self.mesh.keys.network_id)
        candidate = ProxyCandidate(address, name, rssi, data, time.monotonic())
        self.adverts[address] = candidate
        for callback in list(self._callbacks):
            callback(candidate)
        return candidate

    def forget(self, address: str = BULB_MAC) -> None:
        self.adverts.pop(address, None)

    def proxies(self) -> list[ProxyCandidate]:
        return list(self.adverts.values())

    def last_seen(self, address: str) -> ProxyCandidate | None:
        return self.adverts.get(address)

    def subscribe(self, callback: Callable[[ProxyCandidate], None]) -> Callable[[], None]:
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    @property
    def subscribers(self) -> int:
        return len(self._callbacks)

    async def connect(self, address: str) -> LoopbackTransport:
        self.connects.append(address)
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise MeshError(f"cannot connect to {address}")
        if address in self.unreachable or address not in self.adverts:
            raise MeshError(f"cannot connect to {address}")
        transport = self.mesh.connect()
        self.transports.append(transport)
        return transport
