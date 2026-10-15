"""Bring a provisioned node into service: composition, AppKey, binds, publication, range."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import (
    OP_APPKEY_STATUS,
    OP_COMPOSITION_STATUS,
    OP_MODEL_APP_STATUS,
    OP_PUBLICATION_STATUS,
    STATUS_SUCCESS,
    config_appkey_add,
    config_composition_get,
    config_model_app_bind,
    config_model_publication_set,
    parse_composition_status,
    parse_status_code,
)
from .errors import LinkClosedError, MeshError, SeqNotSaved
from .models import (
    KELVIN_MAX,
    KELVIN_MIN,
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_CTL_TEMP_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
    OP_CTL_TEMP_RANGE_STATUS,
    ctl_temperature_range_get,
    parse_ctl_temperature_range_status,
)
from .network import MeshNetwork
from .node import ROLE_CTL, ROLE_CTL_TEMP, ROLE_LIGHTNESS, ROLE_ONOFF, MeshNode
from .proxy import ProxyLink

_LOGGER = logging.getLogger(__name__)

STATUS_KEY_INDEX_ALREADY_STORED = 0x06  # a DIFFERENT key is stored at that index
RANGE_ATTEMPTS = 2

# Generic OnOff first: it is the one model every light and plug has.
BIND_ORDER: tuple[tuple[str, int], ...] = (
    (ROLE_ONOFF, MODEL_GENERIC_ONOFF_SERVER),
    (ROLE_LIGHTNESS, MODEL_LIGHT_LIGHTNESS_SERVER),
    (ROLE_CTL, MODEL_LIGHT_CTL_SERVER),
    (ROLE_CTL_TEMP, MODEL_LIGHT_CTL_TEMP_SERVER),
)


class ConfigureError(MeshError):
    """A required configuration step failed; ``step`` names it."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step


@dataclass
class ConfigureResult:
    bound: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    kelvin_range: tuple[int, int] | None = None


async def configure_node(
    link: ProxyLink, network: MeshNetwork, node: MeshNode, *, publish: bool
) -> ConfigureResult:
    """Read Composition, add our AppKey, bind it, point publication at us, read the range.

    Composition and AppKey Add are required (ConfigureError). Binds, publication and the
    temperature range are best effort: a node that refuses them still works by polling.
    Updates ``node`` in place (element count, CID/PID/VID, models, kelvin range).
    """
    dev_key = node.dev_key
    try:
        reply = await link.request(
            node.unicast, config_composition_get(0), OP_COMPOSITION_STATUS, dev_key=dev_key
        )
        composition = parse_composition_status(reply.params)
    except (MeshError, ValueError) as err:
        raise ConfigureError("composition", str(err)) from err

    try:
        reply = await link.request(
            node.unicast,
            config_appkey_add(network.net_key_index, network.app_key_index, network.app_key),
            OP_APPKEY_STATUS,
            dev_key=dev_key,
        )
        status = parse_status_code(reply.params)
    except MeshError as err:
        raise ConfigureError("appkey", str(err)) from err
    if status == STATUS_KEY_INDEX_ALREADY_STORED:
        # The spec answers Success when the same key is already stored at this index, so 0x06
        # means a different key sits there and the node would never decrypt our commands.
        raise ConfigureError(
            "appkey",
            f"a different AppKey is already stored at index {network.app_key_index}; "
            "reset the device or re-key it",
        )
    if status != STATUS_SUCCESS:
        raise ConfigureError("appkey", f"status 0x{status:02X}")
    node.apply_composition(composition)

    result = ConfigureResult()
    for role, model_id in BIND_ORDER:
        if role not in node.models:
            continue
        element = node.models[role]
        if await _config_step(
            link,
            node,
            config_model_app_bind(element, network.app_key_index, model_id),
            OP_MODEL_APP_STATUS,
            f"bind {role}",
        ):
            result.bound.append(role)

    if publish:
        for role, model_id in BIND_ORDER:
            if role not in result.bound:
                continue
            access = config_model_publication_set(
                node.models[role], network.address, network.app_key_index, model_id
            )
            if await _config_step(link, node, access, OP_PUBLICATION_STATUS, f"publication {role}"):
                result.published.append(role)

    if ROLE_CTL in result.bound or ROLE_CTL_TEMP in result.bound:
        result.kelvin_range = await _read_range(link, node, result.bound)
        if result.kelvin_range is not None:
            node.kelvin_min, node.kelvin_max = result.kelvin_range
    return result


async def _config_step(
    link: ProxyLink, node: MeshNode, access: bytes, opcode: int, what: str
) -> bool:
    try:
        reply = await link.request(node.unicast, access, opcode, dev_key=node.dev_key)
        status = parse_status_code(reply.params)
    except (LinkClosedError, SeqNotSaved):
        raise  # the setup was interrupted, not refused: it must run again on the next connect
    except MeshError as err:
        _LOGGER.warning("Node 0x%04X: %s failed: %s", node.unicast, what, err)
        return False
    if status != STATUS_SUCCESS:
        _LOGGER.warning("Node 0x%04X: %s rejected (status 0x%02X)", node.unicast, what, status)
        return False
    return True


async def _read_range(link: ProxyLink, node: MeshNode, bound: list[str]) -> tuple[int, int] | None:
    """The CTL server owns the range; some Tuya firmware answers on the temperature element."""
    roles = [role for role in (ROLE_CTL, ROLE_CTL_TEMP) if role in bound]
    for address in dict.fromkeys(node.models[role] for role in roles):
        try:
            reply = await link.request(
                address,
                ctl_temperature_range_get(),
                OP_CTL_TEMP_RANGE_STATUS,
                attempts=RANGE_ATTEMPTS,
            )
            status = parse_ctl_temperature_range_status(reply.params)
        except (LinkClosedError, SeqNotSaved):
            raise  # interrupted, not refused
        except MeshError:
            continue
        low = max(KELVIN_MIN, status.min_kelvin)
        high = min(KELVIN_MAX, status.max_kelvin)
        if status.status_code == STATUS_SUCCESS and low < high:
            return low, high
    return None
