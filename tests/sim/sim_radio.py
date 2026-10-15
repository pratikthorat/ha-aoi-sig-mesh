"""A fake Home Assistant Bluetooth radio for SimMesh (implements mesh.connection.MeshRadio)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from mesh.connection import ProxyCandidate
from mesh.errors import MeshError
from mesh.pdu import NetworkKeys
from mesh.provisioner import UnprovisionedDevice
from mesh.proxy import node_identity_data
from tests.sim.mesh_sim import LoopbackTransport, SimMesh
from tests.sim.prov_sim import ProvisioningLoopback, SimUnprovisioned

BULB_MAC = "DC:23:50:00:00:19"


class SimRadio:
    def __init__(self, mesh: SimMesh) -> None:
        self.mesh = mesh
        self.adverts: dict[str, ProxyCandidate] = {}
        self.connects: list[str] = []
        self.transports: list[LoopbackTransport] = []
        self.fail_connects = 0
        self.unreachable: set[str] = set()
        self.provisioning_connects: list[str] = []
        self._unprovisioned: dict[str, tuple[SimUnprovisioned, int | None]] = {}
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
        self._unprovisioned.pop(address, None)

    # provisioning (0x1827)

    def advertise_unprovisioned(self, device: SimUnprovisioned, *, rssi: int | None = -60) -> None:
        """A factory-reset node starts advertising; once provisioned it joins ``self.mesh``
        (if given its NetKey) and advertises the Mesh Proxy service instead."""
        device.on_provisioned = self._provisioned
        self._unprovisioned[device.address] = (device, rssi)
        self.adverts.pop(device.address, None)

    def unprovisioned(self) -> list[UnprovisionedDevice]:
        return [
            UnprovisionedDevice(address, device.name, rssi, device.uuid, device.oob_info)
            for address, (device, rssi) in self._unprovisioned.items()
        ]

    async def connect_provisioning(self, address: str) -> ProvisioningLoopback:
        self.provisioning_connects.append(address)
        found = self._unprovisioned.get(address)
        if found is None or address in self.unreachable:
            raise MeshError(f"cannot connect to {address}")
        return found[0].connect()

    def _provisioned(self, device: SimUnprovisioned) -> None:
        data, dev_key = device.provisioned, device.dev_key
        assert data is not None and dev_key is not None
        _, rssi = self._unprovisioned.pop(device.address)
        self.mesh.add_provisioned(data.unicast, dev_key, data.net_key, device.kind)
        network_id = NetworkKeys.from_net_key(data.net_key).network_id
        self.advertise(device.address, rssi=rssi, network_id=network_id, name=device.name)

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
