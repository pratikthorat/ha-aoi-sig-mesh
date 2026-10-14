"""Configuration model messages (DevKey-encrypted) and composition data."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import MalformedPDUError
from .models import encode_opcode

OP_APPKEY_ADD = 0x00
OP_COMPOSITION_STATUS = 0x02
OP_PUBLICATION_SET = 0x03
OP_APPKEY_STATUS = 0x8003
OP_COMPOSITION_GET = 0x8008
OP_PUBLICATION_STATUS = 0x8019
OP_MODEL_APP_BIND = 0x803D
OP_MODEL_APP_STATUS = 0x803E
OP_NODE_RESET = 0x8049
OP_NODE_RESET_STATUS = 0x804A

STATUS_SUCCESS = 0x00
PUBLISH_TTL_DEFAULT = 0xFF
KEY_INDEX_MAX = 0xFFF


def encode_model_id(model_id: int) -> bytes:
    """SIG model IDs are 16-bit; vendor model IDs are (cid << 16) | model."""
    if 0 <= model_id <= 0xFFFF:
        return struct.pack("<H", model_id)
    if model_id <= 0xFFFFFFFF:
        return struct.pack("<HH", model_id >> 16, model_id & 0xFFFF)
    raise ValueError(f"invalid model ID 0x{model_id:X}")


def pack_key_indexes(net_idx: int, app_idx: int) -> bytes:
    if not (0 <= net_idx <= KEY_INDEX_MAX and 0 <= app_idx <= KEY_INDEX_MAX):
        raise ValueError("key indexes must be 0-0xFFF")
    return (net_idx | (app_idx << 12)).to_bytes(3, "little")


def config_composition_get(page: int = 0) -> bytes:
    return encode_opcode(OP_COMPOSITION_GET) + bytes([page])


def config_appkey_add(net_idx: int, app_idx: int, app_key: bytes) -> bytes:
    if len(app_key) != 16:
        raise ValueError("AppKey must be 16 bytes")
    return encode_opcode(OP_APPKEY_ADD) + pack_key_indexes(net_idx, app_idx) + app_key


def config_model_app_bind(element: int, app_idx: int, model_id: int) -> bytes:
    if not 0 <= app_idx <= KEY_INDEX_MAX:
        raise ValueError("AppKey index must be 0-0xFFF")
    return (
        encode_opcode(OP_MODEL_APP_BIND)
        + struct.pack("<HH", element, app_idx)
        + encode_model_id(model_id)
    )


def config_model_publication_set(
    element: int,
    publish_address: int,
    app_idx: int,
    model_id: int,
    *,
    ttl: int = PUBLISH_TTL_DEFAULT,
    period: int = 0,
    retransmit_count: int = 0,
    retransmit_interval_steps: int = 0,
    credential: bool = False,
) -> bytes:
    if not 0 <= app_idx <= KEY_INDEX_MAX:
        raise ValueError("AppKey index must be 0-0xFFF")
    retransmit = ((retransmit_interval_steps & 0x1F) << 3) | (retransmit_count & 0x07)
    return (
        encode_opcode(OP_PUBLICATION_SET)
        + struct.pack(
            "<HHHBBB",
            element,
            publish_address,
            app_idx | (int(credential) << 12),
            ttl,
            period,
            retransmit,
        )
        + encode_model_id(model_id)
    )


def config_node_reset() -> bytes:
    return encode_opcode(OP_NODE_RESET)


def parse_status_code(params: bytes) -> int:
    if not params:
        raise MalformedPDUError("config status without a status code")
    return params[0]


COMPOSITION_HEADER = struct.Struct("<BHHHHH")  # page, CID, PID, VID, CRPL, features
ELEMENT_HEADER = struct.Struct("<HBB")  # location, NumS, NumV


@dataclass(frozen=True)
class Element:
    location: int
    sig_models: tuple[int, ...]
    vendor_models: tuple[int, ...]


@dataclass(frozen=True)
class CompositionData:
    page: int
    cid: int
    pid: int
    vid: int
    crpl: int
    features: int
    elements: tuple[Element, ...]


def parse_composition_status(params: bytes) -> CompositionData:
    """Parse Config Composition Data Status params (page byte first)."""
    if len(params) < COMPOSITION_HEADER.size:
        raise MalformedPDUError("composition data header too short")
    page, cid, pid, vid, crpl, features = COMPOSITION_HEADER.unpack_from(params)
    offset = COMPOSITION_HEADER.size
    elements = []
    while offset < len(params):
        if offset + ELEMENT_HEADER.size > len(params):
            raise MalformedPDUError("element header cut short")
        location, num_sig, num_vendor = ELEMENT_HEADER.unpack_from(params, offset)
        offset += ELEMENT_HEADER.size
        if offset + num_sig * 2 + num_vendor * 4 > len(params):
            raise MalformedPDUError("element model list cut short")
        sig = struct.unpack_from(f"<{num_sig}H", params, offset)
        offset += num_sig * 2
        vendor = []
        for _ in range(num_vendor):
            vendor_cid, model = struct.unpack_from("<HH", params, offset)
            vendor.append((vendor_cid << 16) | model)
            offset += 4
        elements.append(Element(location, tuple(sig), tuple(vendor)))
    if not elements:
        raise MalformedPDUError("composition data has no elements")
    return CompositionData(page, cid, pid, vid, crpl, features, tuple(elements))


def encode_composition(comp: CompositionData) -> bytes:
    """Encode composition data as Config Composition Data Status params."""
    out = bytearray(
        COMPOSITION_HEADER.pack(comp.page, comp.cid, comp.pid, comp.vid, comp.crpl, comp.features)
    )
    for element in comp.elements:
        out += ELEMENT_HEADER.pack(
            element.location, len(element.sig_models), len(element.vendor_models)
        )
        for model in element.sig_models:
            out += struct.pack("<H", model)
        for model in element.vendor_models:
            out += encode_model_id(model)
    return bytes(out)
