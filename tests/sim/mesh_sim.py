"""In-process SIG Mesh simulator: a GATT proxy plus nodes, using the real mesh crypto."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import struct
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from mesh.config import (
    OP_APPKEY_ADD,
    OP_APPKEY_STATUS,
    OP_COMPOSITION_GET,
    OP_COMPOSITION_STATUS,
    OP_MODEL_APP_BIND,
    OP_MODEL_APP_STATUS,
    OP_NODE_RESET,
    OP_NODE_RESET_STATUS,
    OP_PUBLICATION_SET,
    OP_PUBLICATION_STATUS,
    STATUS_SUCCESS,
    CompositionData,
    Element,
    encode_composition,
)
from mesh.crypto import k4
from mesh.errors import LinkClosedError, MalformedPDUError
from mesh.models import (
    OP_CTL_GET,
    OP_CTL_SET,
    OP_CTL_SET_UNACK,
    OP_CTL_STATUS,
    OP_CTL_TEMP_GET,
    OP_CTL_TEMP_RANGE_GET,
    OP_CTL_TEMP_RANGE_STATUS,
    OP_CTL_TEMP_SET,
    OP_CTL_TEMP_SET_UNACK,
    OP_CTL_TEMP_STATUS,
    OP_HEALTH_ATTENTION_GET,
    OP_HEALTH_ATTENTION_SET,
    OP_HEALTH_ATTENTION_SET_UNACK,
    OP_HEALTH_ATTENTION_STATUS,
    OP_LIGHTNESS_GET,
    OP_LIGHTNESS_SET,
    OP_LIGHTNESS_SET_UNACK,
    OP_LIGHTNESS_STATUS,
    OP_ONOFF_GET,
    OP_ONOFF_SET,
    OP_ONOFF_SET_UNACK,
    OP_ONOFF_STATUS,
    encode_opcode,
    parse_opcode,
)
from mesh.pdu import (
    FILTER_ACCEPT,
    FILTER_REJECT,
    PDU_BEACON,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    PROXY_OP_ADD_ADDRESSES,
    PROXY_OP_FILTER_STATUS,
    PROXY_OP_SET_FILTER_TYPE,
    NetworkKeys,
    NetworkPDU,
    ProxyReassembler,
    decrypt_network_pdu,
    decrypt_proxy_config,
    encrypt_network_pdu,
    encrypt_proxy_config,
    make_secure_beacon,
    proxy_pdus,
)
from mesh.transport import (
    MAX_UNSEGMENTED_ACCESS,
    SEQ_ZERO_MASK,
    SegmentAck,
    SegmentReassembler,
    decrypt_upper,
    encrypt_upper,
    make_segment_ack,
    parse_lower_access,
    parse_segment_ack,
    segmented_access,
    unsegmented_access,
)

NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
CLIENT = 0x0001
SIM_PROXY = 0x7FF0
TUYA_VENDOR_MODEL = (0x07D0 << 16) | 0x0004
KINDS = ("ctl", "lightness", "onoff")
PROXY_OP_REMOVE_ADDRESSES = 0x02
SET_LENGTHS = {  # params of each Set: the short form, or the long form with transition + delay
    **dict.fromkeys((OP_ONOFF_SET, OP_ONOFF_SET_UNACK), (2, 4)),
    **dict.fromkeys((OP_LIGHTNESS_SET, OP_LIGHTNESS_SET_UNACK), (3, 5)),
    **dict.fromkeys((OP_CTL_SET, OP_CTL_SET_UNACK), (7, 9)),
    **dict.fromkeys((OP_CTL_TEMP_SET, OP_CTL_TEMP_SET_UNACK), (5, 7)),
}
STATUS_INVALID_ADDRESS = 0x01
STATUS_INVALID_MODEL = 0x02
DEV_KEY_MODELS = (0x0000, 0x0002)  # Config Server and Health Server: never bound to the AppKey
TUYA_CID_LE = 0xD007  # the Tuya company ID as it appears at the end of a vendor opcode
OPCODE_MODELS = {
    **dict.fromkeys((OP_ONOFF_GET, OP_ONOFF_SET, OP_ONOFF_SET_UNACK), 0x1000),
    **dict.fromkeys((OP_LIGHTNESS_GET, OP_LIGHTNESS_SET, OP_LIGHTNESS_SET_UNACK), 0x1300),
    **dict.fromkeys((OP_CTL_GET, OP_CTL_SET, OP_CTL_SET_UNACK, OP_CTL_TEMP_RANGE_GET), 0x1303),
    **dict.fromkeys((OP_CTL_TEMP_GET, OP_CTL_TEMP_SET, OP_CTL_TEMP_SET_UNACK), 0x1306),
}


def run(coro: Coroutine[Any, Any, Any], timeout: float = 5.0) -> Any:
    """Run a test scenario on a fresh event loop, failing it if it hangs."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _target_model(opcode: int) -> int | None:
    """The server model an app-key opcode is addressed to (None: not binding-checked)."""
    if opcode >= 0xC00000 and opcode & 0xFFFF == TUYA_CID_LE:
        return TUYA_VENDOR_MODEL
    return OPCODE_MODELS.get(opcode)


def _model_id(raw: bytes) -> int:
    if len(raw) == 2:
        return struct.unpack("<H", raw)[0]
    cid, model = struct.unpack("<HH", raw)
    return (cid << 16) | model


@dataclass
class SimNode:
    """One simulated node: Config, Health, OnOff, Lightness, CTL and CTL Temperature servers."""

    unicast: int
    dev_key: bytes = field(repr=False)
    kind: str = "ctl"
    tuya_quirks: bool = False
    has_app_key: bool = True
    on: bool = False
    lightness: int = 0xFFFF
    kelvin: int = 4000
    attention: int = 0
    kelvin_min: int = 2700
    kelvin_max: int = 6500
    reset: bool = False
    bindings: set[tuple[int, int]] = field(default_factory=set)
    publications: dict[tuple[int, int], int] = field(default_factory=dict)
    received: list[tuple[int, int, bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown node kind {self.kind!r}")

    @property
    def element_count(self) -> int:
        return 2 if self.kind == "ctl" else 1

    @property
    def present_lightness(self) -> int:
        return self.lightness if self.on else 0

    def bind_all(self) -> None:
        """Bind every app-key model in the composition, like a node imported already set up."""
        for index, element in enumerate(self.composition().elements):
            for model in element.sig_models + element.vendor_models:
                if model not in DEV_KEY_MODELS:
                    self.bindings.add((self.unicast + index, model))

    def composition(self) -> CompositionData:
        if self.kind == "ctl":
            elements = (
                Element(0, (0x0000, 0x0002, 0x1000, 0x1300, 0x1303), (TUYA_VENDOR_MODEL,)),
                Element(0, (0x1306,), ()),
            )
        elif self.kind == "lightness":
            elements = (Element(0, (0x0000, 0x0002, 0x1000, 0x1300), ()),)
        else:
            elements = (Element(0, (0x0000, 0x1000), ()),)
        return CompositionData(0, 0x07D0, 0x0280, 0x3737, 10, 3, elements)

    def handle(self, dst: int, access: bytes, *, dev_key: bool) -> list[tuple[int, bytes]]:
        """Process one access message; returns the replies as (src, access payload)."""
        if self.reset:
            return []
        opcode, params = parse_opcode(access)
        self.received.append((dst, opcode, params))
        if dev_key:
            if dst != self.unicast:  # the Config Server lives on the primary element only
                return []
            return self._config(opcode, params)
        model = _target_model(opcode)
        if model is not None and (dst, model) not in self.bindings:
            return []
        if opcode in SET_LENGTHS and len(params) not in SET_LENGTHS[opcode]:
            return []
        return self._model(dst - self.unicast, opcode, params)

    def _model_status(self, element: int, model: int) -> int:
        """Config status for a model reference: Success, Invalid Address or Invalid Model."""
        index = element - self.unicast
        if not 0 <= index < self.element_count:
            return STATUS_INVALID_ADDRESS
        found = self.composition().elements[index]
        if model not in found.sig_models + found.vendor_models:
            return STATUS_INVALID_MODEL
        return STATUS_SUCCESS

    def _config(self, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        me = self.unicast
        ok = bytes([STATUS_SUCCESS])
        if opcode == OP_COMPOSITION_GET:
            return [
                (me, encode_opcode(OP_COMPOSITION_STATUS) + encode_composition(self.composition()))
            ]
        if opcode == OP_APPKEY_ADD and len(params) == 19:
            self.has_app_key = True
            return [(me, encode_opcode(OP_APPKEY_STATUS) + ok + params[:3])]
        if opcode == OP_MODEL_APP_BIND and len(params) in (6, 8):
            element = struct.unpack_from("<H", params)[0]
            model = _model_id(params[4:])
            status = self._model_status(element, model)
            if status == STATUS_SUCCESS:
                self.bindings.add((element, model))
            return [(me, encode_opcode(OP_MODEL_APP_STATUS) + bytes([status]) + params)]
        if opcode == OP_PUBLICATION_SET and len(params) in (11, 13):
            element, publish = struct.unpack_from("<HH", params)
            model = _model_id(params[9:])
            status = self._model_status(element, model)
            if status == STATUS_SUCCESS:
                self.publications[(element, model)] = publish
            return [(me, encode_opcode(OP_PUBLICATION_STATUS) + bytes([status]) + params)]
        if opcode == OP_NODE_RESET:
            self.reset = True
            return [(me, encode_opcode(OP_NODE_RESET_STATUS))]
        return []

    def _reply(
        self, opcode: int, unack: int, status: int, params: bytes, src: int | None = None
    ) -> list[tuple[int, bytes]]:
        if opcode == unack:
            return []
        return [(self.unicast if src is None else src, encode_opcode(status) + params)]

    def _clamp_kelvin(self, kelvin: int) -> int:
        return min(max(kelvin, self.kelvin_min), self.kelvin_max)

    def _set_level(self, lightness: int) -> bool:
        """Apply a Lightness or CTL Set; False when a Tuya quirk ignores it."""
        if self.tuya_quirks and (not self.on or lightness == 0):
            return False
        if lightness == 0:
            self.on = False
        else:
            self.on = True
            self.lightness = lightness
        return True

    def _model(self, element: int, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        if element == 1 and self.kind == "ctl":
            return self._ctl_temperature(opcode, params)
        if element != 0:
            return []
        if opcode in (OP_ONOFF_GET, OP_ONOFF_SET, OP_ONOFF_SET_UNACK):
            if opcode != OP_ONOFF_GET:
                self.on = bool(params[0])
            return self._reply(opcode, OP_ONOFF_SET_UNACK, OP_ONOFF_STATUS, bytes([int(self.on)]))
        if self.kind == "onoff":
            return []
        if opcode in (
            OP_HEALTH_ATTENTION_GET,
            OP_HEALTH_ATTENTION_SET,
            OP_HEALTH_ATTENTION_SET_UNACK,
        ):
            if opcode != OP_HEALTH_ATTENTION_GET:
                if not params:
                    return []
                self.attention = params[0]
            return self._reply(
                opcode,
                OP_HEALTH_ATTENTION_SET_UNACK,
                OP_HEALTH_ATTENTION_STATUS,
                bytes([self.attention]),
            )
        if opcode in (OP_LIGHTNESS_GET, OP_LIGHTNESS_SET, OP_LIGHTNESS_SET_UNACK):
            if opcode != OP_LIGHTNESS_GET:
                self._set_level(struct.unpack_from("<H", params)[0])
            return self._reply(
                opcode,
                OP_LIGHTNESS_SET_UNACK,
                OP_LIGHTNESS_STATUS,
                struct.pack("<H", self.present_lightness),
            )
        if self.kind != "ctl":
            return []
        if opcode in (OP_CTL_GET, OP_CTL_SET, OP_CTL_SET_UNACK):
            if opcode != OP_CTL_GET:
                lightness, kelvin = struct.unpack_from("<HH", params)
                if self._set_level(lightness):
                    self.kelvin = self._clamp_kelvin(kelvin)
            return self._reply(
                opcode,
                OP_CTL_SET_UNACK,
                OP_CTL_STATUS,
                struct.pack("<HH", self.present_lightness, self.kelvin),
            )
        if opcode == OP_CTL_TEMP_RANGE_GET:
            params = struct.pack("<BHH", STATUS_SUCCESS, self.kelvin_min, self.kelvin_max)
            return [(self.unicast, encode_opcode(OP_CTL_TEMP_RANGE_STATUS) + params)]
        return []

    def _ctl_temperature(self, opcode: int, params: bytes) -> list[tuple[int, bytes]]:
        if opcode not in (OP_CTL_TEMP_GET, OP_CTL_TEMP_SET, OP_CTL_TEMP_SET_UNACK):
            return []
        if opcode != OP_CTL_TEMP_GET and not (self.tuya_quirks and not self.on):
            self.kelvin = self._clamp_kelvin(struct.unpack_from("<H", params)[0])
        return self._reply(
            opcode,
            OP_CTL_TEMP_SET_UNACK,
            OP_CTL_TEMP_STATUS,
            struct.pack("<Hh", self.kelvin, 0),
            src=self.unicast + 1,
        )


class LoopbackTransport:
    """A fake GATT link to the simulated proxy (implements mesh.proxy.GattTransport)."""

    def __init__(self, mesh: SimMesh, max_write: int = 20) -> None:
        self._mesh = mesh
        self.max_write = max_write
        self.is_connected = True
        self.writes: list[bytes] = []
        self.outbox: list[bytes] = []
        self._notify: Callable[[bytes], None] | None = None
        self._disconnect: Callable[[], None] | None = None

    def set_notify_callback(self, callback: Callable[[bytes], None]) -> None:
        self._notify = callback

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._disconnect = callback

    async def write(self, data: bytes) -> None:
        if not self.is_connected:
            raise LinkClosedError("loopback link is closed")
        self.writes.append(bytes(data))
        self._mesh._on_client_write(self, bytes(data))

    async def close(self) -> None:
        self._disconnected()

    def drop(self) -> None:
        """The proxy went away (out of range, power cut)."""
        self._disconnected()

    def notify(self, data: bytes) -> None:
        """Send one GATT notification to the client on the next loop iteration."""
        if not self.is_connected:
            return
        self.outbox.append(bytes(data))
        asyncio.get_running_loop().call_soon(self._deliver, bytes(data))

    def _deliver(self, data: bytes) -> None:
        if self.is_connected and self._notify is not None:
            self._notify(data)

    def _disconnected(self) -> None:
        if not self.is_connected:
            return
        self.is_connected = False
        if self._disconnect is not None:
            asyncio.get_running_loop().call_soon(self._disconnect)


class SimMesh:
    """A GATT proxy and the nodes behind it."""

    def __init__(
        self,
        net_key: bytes = NET_KEY,
        app_key: bytes = APP_KEY,
        *,
        iv_index: int = 0,
        iv_update: bool = False,
    ) -> None:
        self.keys = NetworkKeys.from_net_key(net_key)
        self.app_key = app_key
        self.aid = k4(app_key)
        self.iv_index = iv_index
        self.iv_update = iv_update
        self.nodes: dict[int, SimNode] = {}
        self.transport: LoopbackTransport | None = None
        self.filter_type = FILTER_ACCEPT
        self.filter_addresses: set[int] = set()
        self.ignore_proxy_config = False
        self.silent = False
        self.drop_from_client = 0
        self.reject_segments = False
        self.drop_acks_to_client = False  # nodes take segments but their acks are lost
        self.accept_replays = False  # opt out of replay protection
        self.from_client: list[NetworkPDU] = []
        self.acks_from_client: list[SegmentAck] = []
        self.last_network: bytes | None = None
        self._seq = itertools.count()
        self._proxy_rx = ProxyReassembler()
        self._segments = SegmentReassembler()
        self._replay: dict[int, tuple[int, int]] = {}  # src -> last accepted (iv_index, seq)

    # nodes and links

    @property
    def tx_iv_index(self) -> int:
        return self.iv_index - 1 if self.iv_update and self.iv_index > 0 else self.iv_index

    def add_node(
        self,
        kind: str = "ctl",
        *,
        unicast: int | None = None,
        tuya_quirks: bool = False,
        configured: bool = True,
    ) -> SimNode:
        if unicast is None:
            unicast = max(
                (node.unicast + node.element_count for node in self.nodes.values()), default=0x00B0
            )
        dev_key = hashlib.sha256(unicast.to_bytes(2, "big")).digest()[:16]
        node = SimNode(
            unicast=unicast,
            dev_key=dev_key,
            kind=kind,
            tuya_quirks=tuya_quirks,
            has_app_key=configured,
        )
        if configured:
            node.bind_all()
        self.nodes[unicast] = node
        return node

    def node_at(self, address: int) -> SimNode | None:
        for node in self.nodes.values():
            if node.unicast <= address < node.unicast + node.element_count:
                return node
        return None

    def dev_key_for(self, address: int) -> bytes | None:
        node = self.node_at(address)
        return node.dev_key if node is not None else None

    def connect(self, max_write: int = 20, *, beacon: bool = True) -> LoopbackTransport:
        """Open a new proxy connection; like real proxies, a beacon follows right away."""
        self.transport = LoopbackTransport(self, max_write)
        self.filter_type = FILTER_ACCEPT
        self.filter_addresses = set()
        self._proxy_rx = ProxyReassembler()
        if beacon:
            self.send_beacon()
        return self.transport

    def forget_replay(self, first: int, count: int) -> None:
        """Clear replay and segment state for srcs first..first+count-1 (re-provisioned)."""
        for src in range(first, first + count):
            self._replay.pop(src, None)
        self._segments.forget(first, count)

    def reset_seq(self) -> None:
        """Nodes restart their seq at 0 when an IV Update completes."""
        self._seq = itertools.count()

    # traffic toward the client

    def send_beacon(
        self,
        *,
        iv_index: int | None = None,
        iv_update: bool | None = None,
        net_key: bytes | None = None,
    ) -> None:
        keys = self.keys if net_key is None else NetworkKeys.from_net_key(net_key)
        beacon = make_secure_beacon(
            keys,
            self.iv_index if iv_index is None else iv_index,
            iv_update=self.iv_update if iv_update is None else iv_update,
        )
        self._notify(PDU_BEACON, beacon)

    def send_access(
        self,
        src: int,
        dst: int,
        access: bytes,
        *,
        dev_key: bytes | None = None,
        segmented: bool = False,
    ) -> None:
        akf = 0 if dev_key else 1
        key = dev_key or self.app_key
        aid = 0 if dev_key else self.aid
        iv = self.tx_iv_index
        seq_auth = next(self._seq)
        upper = encrypt_upper(
            key,
            akf=akf,
            szmic=0,
            seq=seq_auth,
            src=src,
            dst=dst,
            iv_index=iv,
            access_payload=access,
        )
        if not segmented and len(access) <= MAX_UNSEGMENTED_ACCESS:
            self._send_network(src, dst, unsegmented_access(upper, akf=akf, aid=aid), seq=seq_auth)
            return
        lowers = segmented_access(
            upper, akf=akf, aid=aid, szmic=0, seq_zero=seq_auth & SEQ_ZERO_MASK
        )
        for index, lower in enumerate(lowers):
            self._send_network(src, dst, lower, seq=seq_auth if index == 0 else next(self._seq))

    def local_change(self, node: SimNode, on: bool, dst: int = CLIENT) -> None:
        """Someone flipped the physical switch: the node publishes its new OnOff state."""
        node.on = on
        self.send_access(node.unicast, dst, encode_opcode(OP_ONOFF_STATUS) + bytes([int(on)]))

    def replay_last_to_client(self) -> None:
        if self.last_network is not None:
            self._notify(PDU_NETWORK, self.last_network)

    def _notify(self, pdu_type: int, payload: bytes) -> None:
        if self.transport is None or not self.transport.is_connected:
            return
        for part in proxy_pdus(pdu_type, payload, self.transport.max_write):
            self.transport.notify(part)

    def _send_network(
        self, src: int, dst: int, transport_pdu: bytes, *, seq: int, ctl: int = 0
    ) -> None:
        pdu = encrypt_network_pdu(
            self.keys,
            ctl=ctl,
            ttl=4,
            seq=seq,
            src=src,
            dst=dst,
            transport_pdu=transport_pdu,
            iv_index=self.tx_iv_index,
        )
        listed = dst in self.filter_addresses
        if listed != (self.filter_type == FILTER_ACCEPT):
            return  # not on the accept list, or on the reject list
        self.last_network = pdu
        self._notify(PDU_NETWORK, pdu)

    # traffic from the client

    def _rx_iv(self, pdu: bytes) -> int:
        if pdu[0] >> 7 == self.iv_index & 1:
            return self.iv_index
        return max(self.iv_index - 1, 0)

    def _on_client_write(self, transport: LoopbackTransport, data: bytes) -> None:
        if transport is not self.transport:
            return
        message = self._proxy_rx.feed(data)
        if message is None or not message[1]:
            return
        pdu_type, payload = message
        if pdu_type == PDU_PROXY_CONFIG:
            self._on_proxy_config(payload)
        elif pdu_type == PDU_NETWORK:
            self._on_network(payload)

    def _on_proxy_config(self, payload: bytes) -> None:
        net = decrypt_proxy_config(self.keys, payload, self._rx_iv(payload))
        if net is None or self.ignore_proxy_config or not net.transport_pdu:
            return
        message = net.transport_pdu
        opcode = message[0]
        addresses = {int.from_bytes(message[i : i + 2], "big") for i in range(1, len(message), 2)}
        if opcode == PROXY_OP_SET_FILTER_TYPE and len(message) == 2:
            if message[1] not in (FILTER_ACCEPT, FILTER_REJECT):
                return
            self.filter_type = message[1]
            self.filter_addresses = set()
        elif opcode == PROXY_OP_ADD_ADDRESSES and len(message) % 2 == 1:
            self.filter_addresses |= addresses
        elif opcode == PROXY_OP_REMOVE_ADDRESSES and len(message) % 2 == 1:
            self.filter_addresses -= addresses
        else:
            return
        status = bytes([PROXY_OP_FILTER_STATUS, self.filter_type]) + len(
            self.filter_addresses
        ).to_bytes(2, "big")
        pdu = encrypt_proxy_config(
            self.keys,
            seq=next(self._seq),
            src=SIM_PROXY,
            iv_index=self.tx_iv_index,
            message=status,
        )
        self._notify(PDU_PROXY_CONFIG, pdu)

    def _on_network(self, payload: bytes) -> None:
        iv = self._rx_iv(payload)
        net = decrypt_network_pdu(self.keys, payload, iv)
        if net is None:
            return
        if self.drop_from_client > 0:
            self.drop_from_client -= 1
            return
        self.from_client.append(net)
        last = self._replay.get(net.src)
        if not self.accept_replays and last is not None and (iv, net.seq) <= last:
            return  # replayed or older: Mesh Profile 3.8.8
        self._replay[net.src] = (iv, net.seq)
        if self.filter_type == FILTER_ACCEPT:  # Mesh Profile 6.6: the client's src is let through
            self.filter_addresses.add(net.src)
        else:
            self.filter_addresses.discard(net.src)
        if net.ctl:
            ack = parse_segment_ack(net.transport_pdu)
            if ack is not None:
                self.acks_from_client.append(ack)
            return
        node = self.node_at(net.dst)
        if node is None:
            return
        try:
            lower = parse_lower_access(net.transport_pdu)
        except MalformedPDUError:
            return
        if not lower.seg:
            akf, aid, szmic, seq, upper = lower.akf, lower.aid, 0, net.seq, lower.data
        else:
            if self.reject_segments:
                if self.silent:
                    return
                self._send_network(
                    net.dst,
                    net.src,
                    make_segment_ack(lower.seq_zero, 0),
                    seq=next(self._seq),
                    ctl=1,
                )
                return
            result = self._segments.add(net.src, net.seq, lower, iv_index=iv)
            if result.send_ack and not self.silent and not self.drop_acks_to_client:
                ack_pdu = make_segment_ack(result.seq_zero, result.block_ack)
                self._send_network(net.dst, net.src, ack_pdu, seq=next(self._seq), ctl=1)
            if result.message is None:
                return
            done = result.message
            akf, aid, szmic, seq, upper = done.akf, done.aid, done.szmic, done.seq_auth, done.upper
            iv = done.iv_index  # SeqAuth's IV: the IV index of the first segment
        if akf:
            if aid != self.aid or not node.has_app_key:
                return
            key = self.app_key
        else:
            key = node.dev_key
        access = decrypt_upper(
            key, akf=akf, szmic=szmic, seq=seq, src=net.src, dst=net.dst, iv_index=iv, upper=upper
        )
        if access is None:
            return
        was_reset = node.reset
        try:
            replies = node.handle(net.dst, access, dev_key=not akf)
        except MalformedPDUError:
            return
        if node.reset and not was_reset:
            self._segments.forget(node.unicast, node.element_count)
        if self.silent:
            return
        for src, reply in replies:
            self.send_access(src, net.src, reply, dev_key=None if akf else node.dev_key)
