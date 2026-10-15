"""Turn other tools' saved networks into our network + node records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import MeshError
from .models import KELVIN_MAX, KELVIN_MIN
from .node import (
    KELVIN_DEFAULT_MAX,
    KELVIN_DEFAULT_MIN,
    ROLE_CTL,
    ROLE_CTL_TEMP,
    ROLE_LIGHTNESS,
    ROLE_ONOFF,
    UNICAST_MAX,
    MeshNode,
    allocate_unicast,
)

TUYA_DEVICE_TYPE_SIG = "sig_plug"
# Imported networks get a fresh provisioner address from the top of the unicast range, so seq
# 0 is never sent under an address another tool already used.
PROVISIONER_LOW = 0x7F00
PROVISIONER_HIGH = UNICAST_MAX
NODE_RANGE_LOW = 0x0002
NODE_RANGE_HIGH = 0x7EFF
# A node's role addresses must lie within this many elements of its unicast address.
MAX_IMPORT_ELEMENTS = 16
IV_INDEX_MAX = 0xFFFFFFFF


class TuyaImportError(MeshError):
    """The tuya_ble_mesh entry cannot be imported; ``reason`` is a translation key."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class TuyaImport:
    title: str
    net_key: bytes = field(repr=False)
    app_key: bytes = field(repr=False)
    iv_index: int
    node: MeshNode
    address: int
    mac: str
    old_address: int


def is_tuya_sig_entry(data: Mapping[str, Any]) -> bool:
    return data.get("device_type") == TUYA_DEVICE_TYPE_SIG


def _key(data: Mapping[str, Any], name: str) -> bytes:
    try:
        key = bytes.fromhex(str(data[name]))
    except (KeyError, ValueError) as err:
        raise TuyaImportError("bad_keys", f"{name} is missing or not hex") from err
    if len(key) != 16:
        raise TuyaImportError("bad_keys", f"{name} is not 16 bytes")
    return key


def _unicast(data: Mapping[str, Any], name: str, default: str | None = None) -> int:
    try:
        value = int(str(data.get(name, default)), 16)
    except ValueError as err:
        raise TuyaImportError("bad_address", f"{name} is not a hex address") from err
    if not 1 <= value <= UNICAST_MAX:
        raise TuyaImportError("bad_address", f"{name} is not a unicast address")
    return value


def _optional_id(value: Any) -> int | None:
    """A CID/PID/VID as an int (ints or "0x.." / decimal strings), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _kelvin_range(sig: Mapping[str, Any]) -> tuple[int, int]:
    rng = sig.get("temp_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            low = max(KELVIN_MIN, min(int(rng[0]), KELVIN_MAX))
            high = max(KELVIN_MIN, min(int(rng[1]), KELVIN_MAX))
        except (TypeError, ValueError):
            low = high = 0
        if low < high:
            return low, high
    return KELVIN_DEFAULT_MIN, KELVIN_DEFAULT_MAX


def import_tuya_entry(
    title: str,
    data: Mapping[str, Any],
    *,
    used_addresses: Iterable[tuple[int, int]] = (),
) -> TuyaImport:
    """Read a ``tuya_ble_mesh`` "sig_plug" entry.

    Copies the keys, IV index, target unicast, DevKey and the stored ``sig_models`` summary.
    The element count is a first guess: Composition at first connect replaces it. Our own
    address is new: free in ``used_addresses`` (other provisioners we know of), the node and
    the address the fork sent from.
    """
    if not is_tuya_sig_entry(data):
        raise TuyaImportError("not_sig", "only SIG Mesh (sig_plug) entries can be imported")
    net_key = _key(data, "net_key")
    app_key = _key(data, "app_key")
    dev_key = _key(data, "dev_key")
    unicast = _unicast(data, "unicast_target")
    old_address = _unicast(data, "unicast_our", "0001")
    iv_index = data.get("iv_index", 0)
    if (
        not isinstance(iv_index, int)
        or isinstance(iv_index, bool)
        or not 0 <= iv_index <= IV_INDEX_MAX
    ):
        raise TuyaImportError("bad_iv_index", "iv_index is not an integer from 0 to 0xFFFFFFFF")
    mac = str(data.get("mac_address", "")).upper()

    sig = data.get("sig_models")
    sig = sig if isinstance(sig, Mapping) else {}
    try:
        elements = int(sig.get("elements", 1))
    except (TypeError, ValueError):
        elements = 1
    models: dict[str, int] = {}
    for role in (ROLE_ONOFF, ROLE_LIGHTNESS, ROLE_CTL, ROLE_CTL_TEMP):
        if role in sig:
            try:
                models[role] = int(sig[role])
            except (TypeError, ValueError):
                continue
    models.setdefault(ROLE_ONOFF, unicast)
    for role, address in models.items():
        if not unicast <= address < unicast + MAX_IMPORT_ELEMENTS:
            raise TuyaImportError(
                "bad_address", f"{role} address 0x{address:04X} is not within the node"
            )
    elements = max(1, elements, max(models.values()) - unicast + 1)
    kelvin_min, kelvin_max = _kelvin_range(sig)
    try:
        node = MeshNode(
            unicast=unicast,
            element_count=elements,
            dev_key=dev_key,
            cid=_optional_id(sig.get("cid")),
            pid=_optional_id(sig.get("pid")),
            vid=_optional_id(sig.get("vid")),
            models=models,
            kelvin_min=kelvin_min,
            kelvin_max=kelvin_max,
        )
    except ValueError as err:
        raise TuyaImportError("bad_address", str(err)) from err

    taken = [*used_addresses, (unicast, elements), (old_address, 1)]
    try:
        address = allocate_unicast(taken, 1, low=PROVISIONER_LOW, high=PROVISIONER_HIGH)
    except MeshError as err:
        raise TuyaImportError("no_address", str(err)) from err
    return TuyaImport(
        title=title,
        net_key=net_key,
        app_key=app_key,
        iv_index=iv_index,
        node=node,
        address=address,
        mac=mac,
        old_address=old_address,
    )
