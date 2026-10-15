"""Turn other tools' saved networks into our network + node records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import MeshError
from .models import KELVIN_MAX, KELVIN_MIN
from .node import (
    KELVIN_DEFAULT_MAX,
    KELVIN_DEFAULT_MIN,
    MODEL_ROLES,
    ROLE_CTL,
    ROLE_CTL_TEMP,
    ROLE_HEALTH,
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
# A node's declared element count, and its role addresses' offset from its unicast address,
# must stay within this many elements.
MAX_IMPORT_ELEMENTS = 16
IV_INDEX_MAX = 0xFFFFFFFF
_MAC_SEPARATED = re.compile(r"[0-9A-F]{2}([:-])(?:[0-9A-F]{2}\1){4}[0-9A-F]{2}", re.IGNORECASE)
_MAC_BARE = re.compile(r"[0-9A-F]{12}", re.IGNORECASE)


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


def _mac(data: Mapping[str, Any]) -> str:
    """The Bluetooth address as "AA:BB:CC:DD:EE:FF": 6 octets joined by one of ':' '-', or bare."""
    value = data.get("mac_address")
    if not isinstance(value, str) or not (
        _MAC_SEPARATED.fullmatch(value) or _MAC_BARE.fullmatch(value)
    ):
        raise TuyaImportError("bad_mac", "mac_address is missing or not a Bluetooth address")
    digits = re.sub(r"[:-]", "", value).upper()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


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
        except (TypeError, ValueError, OverflowError):
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
    mac = _mac(data)

    sig = data.get("sig_models")
    sig = sig if isinstance(sig, Mapping) else {}
    try:
        elements = int(sig.get("elements", 1))
    except (TypeError, ValueError, OverflowError):
        elements = 1
    if elements > MAX_IMPORT_ELEMENTS:
        # A huge count would reserve a big range around the node, which could leave no
        # provisioner address free.
        raise TuyaImportError(
            "bad_address", f"{elements} elements is more than {MAX_IMPORT_ELEMENTS}"
        )
    models: dict[str, int] = {}
    for role in (ROLE_ONOFF, ROLE_LIGHTNESS, ROLE_CTL, ROLE_CTL_TEMP):
        if role in sig:
            try:
                models[role] = int(sig[role])
            except (TypeError, ValueError, OverflowError):
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


# nRF Mesh JSON (the Mesh Configuration Database format)

NRF_DEFAULT_TITLE = "nRF Mesh network"
# The SIG models we drive: a node with none of them is not imported.
DRIVABLE_ROLES = (ROLE_ONOFF, ROLE_LIGHTNESS, ROLE_CTL)


class NrfImportError(MeshError):
    """The nRF Mesh export cannot be imported; ``reason`` is a translation key."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class NrfNode:
    title: str
    node: MeshNode
    configured: bool  # every role model is already bound to our AppKey
    mac: str | None = None  # an export has no Bluetooth addresses


@dataclass
class NrfImport:
    title: str
    net_key: bytes = field(repr=False)
    net_key_index: int
    app_key: bytes = field(repr=False)
    app_key_index: int
    iv_index: int
    address: int
    range_low: int
    range_high: int
    nodes: list[NrfNode]
    skipped: int  # nodes left out: excluded, no DevKey, or nothing we can drive


def _nrf_list(data: Mapping[str, Any], name: str) -> list[Any]:
    value = data.get(name, [])
    if not isinstance(value, list):
        raise NrfImportError("bad_json", f"{name} is not a list")
    return value


def _nrf_key(value: Any, name: str) -> bytes:
    try:
        key = bytes.fromhex(value) if isinstance(value, str) else b""
    except ValueError:
        key = b""
    if len(key) != 16:
        raise NrfImportError("bad_keys", f"{name} is missing or not 16 hex bytes")
    return key


def _nrf_index(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0x0FFF:
        raise NrfImportError("bad_keys", f"{name} is not a key index")
    return value


def _nrf_unicast(value: Any, name: str) -> int:
    try:
        address = int(value, 16) if isinstance(value, str) else -1
    except ValueError:
        address = -1
    if not 1 <= address <= UNICAST_MAX:
        raise NrfImportError("bad_address", f"{name} is not a unicast address")
    return address


def _nrf_hex_id(value: Any) -> int | None:
    try:
        return int(value, 16) if isinstance(value, str) else None
    except ValueError:
        return None


def _nrf_iv_index(data: Mapping[str, Any]) -> int:
    """``ivIndex`` as a number or {"index": n}; 0 when the export has none (beacons tell)."""
    value = data.get("ivIndex", 0)
    if isinstance(value, Mapping):
        value = value.get("index", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= IV_INDEX_MAX:
        raise NrfImportError("bad_iv_index", "ivIndex is not an integer from 0 to 0xFFFFFFFF")
    return value


def _free_gaps(taken: Iterable[tuple[int, int]], low: int, high: int) -> list[tuple[int, int]]:
    """The free (first, last) runs in [low, high] around the taken (start, count) blocks."""
    gaps, candidate = [], low
    for start, count in sorted(taken):
        if start > candidate:
            gaps.append((candidate, min(start - 1, high)))
        candidate = max(candidate, start + count)
        if candidate > high:
            break
    if candidate <= high:
        gaps.append((candidate, high))
    return [(first, last) for first, last in gaps if first <= last]


def _nrf_node(
    raw: Mapping[str, Any], app_key_index: int, provisioner_uuids: set[str]
) -> tuple[int, int, NrfNode | None]:
    """(unicast, element count, the node or None when it is not imported)."""
    if not isinstance(raw, Mapping):
        raise NrfImportError("bad_json", "a node is not an object")
    unicast = _nrf_unicast(raw.get("unicastAddress"), "a node's unicastAddress")
    elements = raw.get("elements") or []
    if not isinstance(elements, list) or not all(isinstance(e, Mapping) for e in elements):
        raise NrfImportError("bad_json", f"elements of node 0x{unicast:04X} are not a list")
    count = max(1, len(elements))
    if unicast + count - 1 > UNICAST_MAX:
        raise NrfImportError("bad_address", f"node 0x{unicast:04X} leaves the unicast range")
    if str(raw.get("UUID", "")).upper() in provisioner_uuids:
        return unicast, count, None  # a provisioner (a phone): not a device
    try:
        dev_key = _nrf_key(raw.get("deviceKey"), "deviceKey")
    except NrfImportError:
        dev_key = b""
    if raw.get("excluded") is True or not dev_key:
        return unicast, count, None

    models: dict[str, int] = {}
    bound: dict[str, bool] = {}
    for position, element in enumerate(elements):
        index = element.get("index", position)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
            index = position
        for model in element.get("models") or []:
            if not isinstance(model, Mapping):
                continue
            model_id = _nrf_hex_id(model.get("modelId"))
            if model_id is None or len(str(model.get("modelId"))) != 4:
                continue  # vendor models (8 hex digits) are never ours to drive
            role = MODEL_ROLES.get(model_id)
            if role is None or role in models:
                continue
            models[role] = unicast + index
            binds = model.get("bind")
            bound[role] = isinstance(binds, list) and app_key_index in binds
    if elements and not any(role in models for role in DRIVABLE_ROLES):
        return unicast, count, None
    configured = bool(elements) and all(bound[role] for role in models if role != ROLE_HEALTH)
    if not elements:
        models = {ROLE_ONOFF: unicast}  # composition unknown: first connect reads it
    node = MeshNode(
        unicast=unicast,
        element_count=count,
        dev_key=dev_key,
        cid=_nrf_hex_id(raw.get("cid")),
        pid=_nrf_hex_id(raw.get("pid")),
        vid=_nrf_hex_id(raw.get("vid")),
        models=models,
    )
    name = raw.get("name")
    title = name.strip() if isinstance(name, str) and name.strip() else f"Node 0x{unicast:04X}"
    return unicast, count, NrfNode(title=title, node=node, configured=configured)


def import_nrf_json(text: str, *, used_addresses: Iterable[tuple[int, int]] = ()) -> NrfImport:
    """Read an nRF Mesh export (Mesh Configuration Database JSON).

    Uses ``netKeys[0]`` and the first AppKey bound to it. Our own address is free of every
    provisioner's unicast range, every node, every excluded address and ``used_addresses``
    (addresses we sent from before), preferring 0x7F00-0x7FFF. Devices we add later go in the
    largest run of addresses no provisioner owns.
    """
    try:
        data = json.loads(text)
    except ValueError as err:
        raise NrfImportError("bad_json", f"not JSON: {err.msg}") from None
    if not isinstance(data, dict):
        raise NrfImportError("bad_json", "not a JSON object")

    net_keys = data.get("netKeys")
    if not isinstance(net_keys, list) or not net_keys or not isinstance(net_keys[0], Mapping):
        raise NrfImportError("bad_keys", "netKeys is missing")
    net = net_keys[0]
    net_key = _nrf_key(net.get("key"), "netKeys[0].key")
    net_key_index = _nrf_index(net.get("index", 0), "netKeys[0].index")
    if net.get("phase", 0) != 0:
        raise NrfImportError("key_refresh", "netKeys[0] is in the middle of a key refresh")
    app = next(
        (
            a
            for a in _nrf_list(data, "appKeys")
            if isinstance(a, Mapping) and a.get("boundNetKey") == net_key_index
        ),
        None,
    )
    if app is None:
        raise NrfImportError("no_app_key", "no AppKey is bound to netKeys[0]")
    app_key = _nrf_key(app.get("key"), "the AppKey")
    app_key_index = _nrf_index(app.get("index", 0), "the AppKey index")
    iv_index = _nrf_iv_index(data)

    ranges: list[tuple[int, int]] = []
    provisioner_uuids: set[str] = set()
    for provisioner in _nrf_list(data, "provisioners"):
        if not isinstance(provisioner, Mapping):
            raise NrfImportError("bad_json", "a provisioner is not an object")
        provisioner_uuids.add(str(provisioner.get("UUID", "")).upper())
        for rng in provisioner.get("allocatedUnicastRange") or []:
            if not isinstance(rng, Mapping):
                raise NrfImportError("bad_json", "a unicast range is not an object")
            low = _nrf_unicast(rng.get("lowAddress"), "lowAddress")
            high = _nrf_unicast(rng.get("highAddress"), "highAddress")
            if low <= high:
                ranges.append((low, high - low + 1))
    provisioner_uuids.discard("")

    taken: list[tuple[int, int]] = [*ranges, *used_addresses]
    nodes: list[NrfNode] = []
    skipped = 0
    for raw in _nrf_list(data, "nodes"):
        unicast, count, node = _nrf_node(raw, app_key_index, provisioner_uuids)
        taken.append((unicast, count))
        if node is not None:
            nodes.append(node)
        elif str(raw.get("UUID", "")).upper() not in provisioner_uuids:
            skipped += 1
    for exclusion in _nrf_list(data, "networkExclusions"):
        addresses = exclusion.get("addresses", []) if isinstance(exclusion, Mapping) else []
        for value in addresses if isinstance(addresses, list) else []:
            try:
                taken.append((_nrf_unicast(value, "an excluded address"), 1))
            except NrfImportError:
                continue  # group or malformed entries do not block a unicast

    try:
        address = allocate_unicast(taken, 1, low=PROVISIONER_LOW, high=PROVISIONER_HIGH)
    except MeshError:
        try:
            address = allocate_unicast(taken, 1)
        except MeshError as err:
            raise NrfImportError("no_address", str(err)) from err
    gaps = _free_gaps([*ranges, (address, 1)], 0x0001, UNICAST_MAX)
    if not gaps:
        raise NrfImportError("no_address", "every unicast address belongs to a provisioner")
    range_low, range_high = max(gaps, key=lambda gap: (gap[1] - gap[0], -gap[0]))
    title = data.get("meshName")
    return NrfImport(
        title=title.strip() if isinstance(title, str) and title.strip() else NRF_DEFAULT_TITLE,
        net_key=net_key,
        net_key_index=net_key_index,
        app_key=app_key,
        app_key_index=app_key_index,
        iv_index=iv_index,
        address=address,
        range_low=range_low,
        range_high=range_high,
        nodes=nodes,
        skipped=skipped,
    )
