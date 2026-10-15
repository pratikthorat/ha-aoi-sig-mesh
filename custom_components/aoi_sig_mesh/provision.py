"""Provisioning a factory-reset device into one of our networks (for the config flows)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_ADDRESS,
    CONF_IV_INDEX,
    CONF_NET_KEY,
    CONF_NET_KEY_INDEX,
    CONF_RANGE_HIGH,
    CONF_RANGE_LOW,
    DOMAIN,
    SUBENTRY_NODE,
)
from .coordinator import claimed_range
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
from .store import AddressBook, NetworkStore

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


def _busy(hass: HomeAssistant) -> set[str]:
    return hass.data.setdefault(DOMAIN, {}).setdefault("provisioning", set())


def claim_device(hass: HomeAssistant, address: str) -> bool:
    """Mark ``address`` as being provisioned; False if another flow already is."""
    busy = _busy(hass)
    if address in busy:
        return False
    busy.add(address)
    return True


def release_device(hass: HomeAssistant, address: str) -> None:
    _busy(hass).discard(address)


def entry_taken(entry: ConfigEntry) -> list[tuple[int, int]]:
    """Our address and every node block of ``entry``, as damaged data allows."""
    taken = [(int(entry.data[CONF_ADDRESS]), 1)]
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_NODE and (block := claimed_range(subentry.data)):
            taken.append(block)
    return taken


async def async_entry_iv(hass: HomeAssistant, entry: ConfigEntry) -> tuple[int, bool]:
    """(IV index, IV Update) a new device must be given: the running network's, else the
    saved state's, else the entry's."""
    if entry.state is ConfigEntryState.LOADED:
        network = entry.runtime_data.network
        return network.iv_index, network.iv_update
    state = await NetworkStore(hass, entry.entry_id).async_load()
    if state is not None:
        return int(state["iv_index"]), bool(state.get("iv_update", False))
    return int(entry.data.get(CONF_IV_INDEX, 0)), False


async def async_provision_into_entry(
    hass: HomeAssistant, entry: ConfigEntry, device: UnprovisionedDevice
) -> ProvisioningResult:
    """Provision ``device`` into the network of ``entry`` (loaded or not)."""
    iv_index, iv_update = await async_entry_iv(hass, entry)
    return await async_provision_device(
        hass, entry.data, device, iv_index=iv_index, iv_update=iv_update, taken=entry_taken(entry)
    )


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
