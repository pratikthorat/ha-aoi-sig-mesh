"""Provisioning a factory-reset device into one of our networks (for the config flows)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_NET_KEY, CONF_NET_KEY_INDEX, CONF_RANGE_HIGH, CONF_RANGE_LOW
from .mesh.errors import MeshError, ProvisioningError
from .mesh.pdu import NetworkKeys
from .mesh.provisioner import (
    FLAG_IV_UPDATE,
    Capabilities,
    OobRequired,
    ProvisioningRefused,
    ProvisioningResult,
    UnprovisionedDevice,
    provision_device,
)
from .radio import HaBluetoothRadio
from .store import AddressBook

_LOGGER = logging.getLogger(__name__)

# Once the Data PDU is on its way, the device may hold our keys and its address.
HALF_PROVISIONED_STEPS = ("data", "complete")


class ProvisionFailed(Exception):
    """Provisioning did not finish. ``reason`` is a translation key; ``step`` and ``detail``
    fill its placeholders (never key material)."""

    def __init__(self, reason: str, step: str, detail: str) -> None:
        super().__init__(reason, step, detail)
        self.reason = reason
        self.step = step
        self.detail = detail


def async_unprovisioned(hass: HomeAssistant) -> list[UnprovisionedDevice]:
    """Factory-reset nodes in range of any connectable adapter or proxy, strongest first."""
    return HaBluetoothRadio(hass).unprovisioned()


async def async_provision_device(
    hass: HomeAssistant,
    data: Mapping[str, Any],
    device: UnprovisionedDevice,
    *,
    iv_index: int,
    iv_update: bool = False,
    taken: list[tuple[int, int]],
) -> ProvisioningResult:
    """Provision ``device`` into the network whose entry data is ``data``.

    ``taken`` lists the network's node blocks and our own address. The device's address is
    reserved in the AddressBook before the device is sent its keys, and stays reserved
    whatever happens next. Raises ProvisionFailed.
    """
    net_key = bytes.fromhex(data[CONF_NET_KEY])
    network_id = NetworkKeys.from_net_key(net_key).network_id.hex()
    book = AddressBook(hass)

    async def assign(caps: Capabilities) -> int:
        try:
            return await book.async_reserve_node(
                network_id,
                caps.elements,
                taken=taken,
                low=int(data[CONF_RANGE_LOW]),
                high=int(data[CONF_RANGE_HIGH]),
            )
        except (MeshError, ValueError) as err:  # range full, or a malformed range
            raise ProvisionFailed("no_address", "capabilities", str(err)) from err
        except (OSError, HomeAssistantError) as err:  # the strict save (WriteError) failed
            raise ProvisionFailed("cannot_save", "capabilities", str(err)) from err

    try:
        return await provision_device(
            HaBluetoothRadio(hass),
            device.address,
            net_key=net_key,
            net_key_index=int(data.get(CONF_NET_KEY_INDEX, 0)),
            iv_index=iv_index,
            flags=FLAG_IV_UPDATE if iv_update else 0,
            assign=assign,
        )
    except ProvisioningError as err:
        if isinstance(err, OobRequired):
            reason = "oob_required"
        elif err.step in HALF_PROVISIONED_STEPS and not isinstance(err, ProvisioningRefused):
            reason = "half_provisioned"
        elif device.remote:
            reason = "provision_failed_remote"
        else:
            reason = "provision_failed"
        _LOGGER.warning("Provisioning %s failed at %s: %s", device.address, err.step, err.message)
        raise ProvisionFailed(reason, err.step, err.message) from err
