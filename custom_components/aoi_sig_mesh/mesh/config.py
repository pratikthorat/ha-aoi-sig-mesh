"""Configuration model messages (DevKey-encrypted) and composition data."""

from __future__ import annotations

import struct

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
