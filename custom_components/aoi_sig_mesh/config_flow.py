"""Config flow: one entry per mesh network, one "node" subentry per device."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
    UnknownEntry,
)
from homeassistant.const import ATTR_RESTORED, Platform
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ADDRESS,
    CONF_APP_KEY,
    CONF_APP_KEY_INDEX,
    CONF_CONFIGURED,
    CONF_DEVICE,
    CONF_DISABLE_OLD,
    CONF_IDLE_TIMEOUT,
    CONF_IV_INDEX,
    CONF_MAC,
    CONF_NAME,
    CONF_NET_KEY,
    CONF_NET_KEY_INDEX,
    CONF_NETWORK,
    CONF_NRF_JSON,
    CONF_PUBLISH_SETUP,
    CONF_RANGE_HIGH,
    CONF_RANGE_LOW,
    CONF_REMOVE_ANYWAY,
    CONF_ROLE,
    CONF_SOURCE_ENTRY,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_NETWORK_NAME,
    DEFAULT_PUBLISH_SETUP,
    DOMAIN,
    NEW_NETWORK_ADDRESS,
    PROVISIONING_SERVICE_UUID,
    PROXY_SERVICE_UUID,
    ROLE_AUTO,
    ROLE_LIGHT,
    ROLE_SWITCH,
    SUBENTRY_NODE,
    TUYA_DOMAIN,
)
from .coordinator import claimed_range, default_role, node_role, read_node
from .mesh.importers import (
    NODE_RANGE_HIGH,
    NODE_RANGE_LOW,
    NrfImport,
    NrfImportError,
    NrfNode,
    TuyaImport,
    TuyaImportError,
    import_nrf_json,
    import_tuya_entry,
    is_tuya_sig_entry,
)
from .mesh.node import MeshNode
from .mesh.pdu import NetworkKeys
from .mesh.provisioner import (
    ProvisioningResult,
    UnprovisionedDevice,
    parse_provisioning_service_data,
)
from .mesh.proxy import parse_proxy_service_data
from .provision import (
    ProvisionFailed,
    async_provision_device,
    async_provision_into_entry,
    async_unprovisioned,
    claim_device,
    release_device,
)
from .store import AddressBook

IDLE_TIMEOUT_MAX = 240  # minutes
SWAPPED_DOMAINS = (Platform.LIGHT, Platform.SWITCH)  # a node's main entity

_LOGGER = logging.getLogger(__name__)


def node_subentry_data(node_import: TuyaImport) -> dict[str, Any]:
    node = node_import.node
    return {
        **node.to_dict(),
        CONF_ROLE: default_role(node),
        CONF_MAC: node_import.mac,
        CONF_CONFIGURED: False,
    }


def node_unique_id(unicast: int) -> str:
    return f"{unicast:04x}"


def device_label(device: UnprovisionedDevice) -> str:
    rssi = f", {device.rssi} dBm" if device.rssi is not None else ""
    return f"{device.name or 'Unnamed device'} ({device.address}{rssi})"


def device_title(device: UnprovisionedDevice) -> str:
    return device.name or f"Mesh device {device.address[-5:]}"


def provisioned_subentry_data(
    result: ProvisioningResult, role: str | None, mac: str
) -> dict[str, Any]:
    """A device we just provisioned: models unknown until first connect reads Composition and
    sets it up. ``role`` None follows the node (light if it has Lightness, else switch)."""
    node = MeshNode(
        unicast=result.unicast,
        element_count=result.element_count,
        dev_key=result.dev_key,
        models={},
    )
    return {**node.to_dict(), CONF_ROLE: role, CONF_MAC: mac, CONF_CONFIGURED: False}


def network_data(
    name: str,
    net_key: bytes,
    app_key: bytes,
    address: int,
    *,
    iv_index: int = 0,
    net_key_index: int = 0,
    app_key_index: int = 0,
    range_low: int = NODE_RANGE_LOW,
    range_high: int = NODE_RANGE_HIGH,
) -> dict[str, Any]:
    """A network entry's data. Keys are stored as hex, as every importer stores them."""
    return {
        CONF_NAME: name,
        CONF_NET_KEY: net_key.hex(),
        CONF_NET_KEY_INDEX: net_key_index,
        CONF_APP_KEY: app_key.hex(),
        CONF_APP_KEY_INDEX: app_key_index,
        CONF_ADDRESS: address,
        CONF_RANGE_LOW: range_low,
        CONF_RANGE_HIGH: range_high,
        CONF_IV_INDEX: iv_index,
    }


DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_IDLE_TIMEOUT: DEFAULT_IDLE_TIMEOUT,
    CONF_PUBLISH_SETUP: DEFAULT_PUBLISH_SETUP,
}


def nrf_subentry(nrf_node: NrfNode) -> dict[str, Any]:
    """A node from an nRF Mesh export. Only a node with a role model not bound to our AppKey
    still needs setup; the rest is used as the export describes it."""
    node = nrf_node.node
    return {
        "subentry_type": SUBENTRY_NODE,
        "title": nrf_node.title,
        "unique_id": node_unique_id(node.unicast),
        "data": {
            **node.to_dict(),
            CONF_ROLE: default_role(node),
            CONF_MAC: nrf_node.mac,
            CONF_CONFIGURED: nrf_node.configured,
        },
    }


def forget_replay(entry: ConfigEntry, result: ProvisioningResult) -> None:
    """The address is new to the network, but a replay entry may outlive a device removed
    while the network was unloaded: the new device starts again at seq 0."""
    if entry.state is ConfigEntryState.LOADED:
        entry.runtime_data.async_forget_node(result.unicast, result.element_count)


ADD_ROLES = [ROLE_AUTO, ROLE_LIGHT, ROLE_SWITCH]


def role_selector() -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=ADD_ROLES, translation_key="add_role"))


class ProvisionSteps:
    """The steps Add device and Bluetooth discovery share.

    The flow sets ``_device`` and ``_role`` (``_set_choice``), then calls
    ``async_step_provision``; it supplies ``_async_provision`` and ``async_step_finish``. A
    failure goes back to the step named ``_form_step``, which shows it with ``_take_failure``.
    """

    hass: HomeAssistant
    _form_step = "user"

    def __init__(self) -> None:
        self._device: UnprovisionedDevice | None = None
        self._role: str | None = None
        self._task: asyncio.Task[ProvisioningResult] | None = None
        self._result: ProvisioningResult | None = None
        self._failure: ProvisionFailed | None = None

    def _take_failure(self) -> tuple[dict[str, str], dict[str, str]]:
        """(errors, placeholders) for the form: the last failure, shown once."""
        failure, self._failure = self._failure, None
        if failure is None:
            return {}, {"step": "", "detail": ""}
        return {"base": failure.reason}, {"step": failure.step, "detail": failure.detail}

    def _set_choice(self, device: UnprovisionedDevice, role: str) -> None:
        self._device = device
        self._role = None if role == ROLE_AUTO else role

    async def _async_provision(self, device: UnprovisionedDevice) -> ProvisioningResult:
        raise NotImplementedError

    async def _async_form(self) -> Any:
        return await getattr(self, f"async_step_{self._form_step}")()

    async def async_step_provision(self, user_input: dict[str, Any] | None = None) -> Any:
        """Give the device the network's keys and an address of its own (PB-GATT)."""
        assert self._device is not None
        if self._task is None:
            address = self._device.address
            if not claim_device(self.hass, address):
                self._failure = ProvisionFailed("already_provisioning", "", "")
                return await self._async_form()
            self._task = self.hass.async_create_task(
                self._async_provision(self._device), "aoi_sig_mesh provisioning"
            )
            # runs on success, failure and cancel (the dialog was closed)
            self._task.add_done_callback(lambda _task: release_device(self.hass, address))
        if not self._task.done():
            return self.async_show_progress(  # type: ignore[attr-defined]
                step_id="provision",
                progress_action="provision",
                progress_task=self._task,
                description_placeholders={"name": device_title(self._device)},
            )
        task, self._task = self._task, None
        try:
            self._result = task.result()
        except ProvisionFailed as err:
            self._failure = err
            if err.reason == "half_provisioned":
                return self.async_show_progress_done(  # type: ignore[attr-defined]
                    next_step_id="half_provisioned"
                )
            return self.async_show_progress_done(  # type: ignore[attr-defined]
                next_step_id=self._form_step
            )
        except Exception as err:  # noqa: BLE001 - never leave the dialog spinning
            _LOGGER.exception("Adding %s failed unexpectedly", self._device.address)
            self._failure = ProvisionFailed("unknown", "", type(err).__name__)
            return self.async_show_progress_done(  # type: ignore[attr-defined]
                next_step_id=self._form_step
            )
        return self.async_show_progress_done(next_step_id="finish")  # type: ignore[attr-defined]

    async def async_step_half_provisioned(self, user_input: dict[str, Any] | None = None) -> Any:
        """The device may hold our keys now but did not confirm: only a reset recovers it."""
        assert self._failure is not None and self._device is not None
        return self.async_abort(  # type: ignore[attr-defined]
            reason="half_provisioned",
            description_placeholders={
                "name": device_title(self._device),
                "step": self._failure.step,
            },
        )


class AoiSigMeshConfigFlow(ProvisionSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _form_step = "device"

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None  # a discovered factory-reset device
        self._target: ConfigEntry | None = None  # the network it joins, or None for a new one
        self._new_network: dict[str, Any] | None = None

        self._import: TuyaImport | None = None
        self._source: ConfigEntry | None = None
        self._existing: ConfigEntry | None = None
        self._name = DEFAULT_NETWORK_NAME
        self._disable_old = False
        self._ignored: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AoiSigMeshOptionsFlow:
        return AoiSigMeshOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_NODE: NodeSubentryFlow}

    def _tuya_entries(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self.hass.config_entries.async_entries(TUYA_DOMAIN)
            if is_tuya_sig_entry(entry.data)
        ]

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """A factory-reset device (0x1827) is offered for adding. A mesh proxy advert (0x1828)
        is offered only for a network we can import (and do not have)."""
        if (
            unprovisioned := discovery_info.service_data.get(PROVISIONING_SERVICE_UUID)
        ) is not None:
            parsed = parse_provisioning_service_data(unprovisioned)
            if parsed is None:
                return self.async_abort(reason="not_supported")
            # One flow per device (its UUID); ignoring it hides only that device.
            await self.async_set_unique_id(parsed[0].hex())
            self._abort_if_unique_id_configured()
            self._address = discovery_info.address.upper()
            self.context["title_placeholders"] = {"name": discovery_info.name or self._address}
            return await self.async_step_device()
        data = discovery_info.service_data.get(PROXY_SERVICE_UUID)
        advert = parse_proxy_service_data(data) if data else None
        if advert is None or advert.network_id is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(advert.network_id.hex())
        self._abort_if_unique_id_configured()
        if not self._tuya_entries():
            return self.async_abort(reason="not_supported")
        # A network that already exists: making a new one would not get its devices.
        return self.async_show_menu(step_id="user", menu_options=["tuya_import"])

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["create", "nrf_import"]
        if self._tuya_entries():
            options.append("tuya_import")
        return self.async_show_menu(step_id="user", menu_options=options)

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add the discovered device to a network we have, or to a new one made for it."""
        failure = self._failure
        errors, placeholders = self._take_failure()
        device = next(
            (d for d in async_unprovisioned(self.hass) if d.address == self._address), None
        )
        if device is None:  # added meanwhile (Add device), reset into another network, or gone
            if failure is not None:  # the real reason, not "gone"
                return self.async_abort(
                    reason=failure.reason,
                    description_placeholders={"step": failure.step, "detail": failure.detail},
                )
            return self.async_abort(reason="device_gone")
        networks = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.source != SOURCE_IGNORE
        ]
        # HA passes the submitted values on again when progress ends in this step: a pending
        # failure means they were already used, so only show the form.
        if user_input is not None and failure is None:
            # Branch on what was submitted, not on the live list: the form may be older than
            # a network that was deleted or made since. A submit of the wrong shape only
            # shows the form again.
            if CONF_NETWORK in user_input:
                target = self.hass.config_entries.async_get_entry(user_input[CONF_NETWORK])
                if target is None or target.source == SOURCE_IGNORE:
                    return self.async_abort(
                        reason="network_gone"
                    )  # deleted while the form was open
                self._set_choice(device, user_input[CONF_ROLE])
                self._target = target
                return await self.async_step_provision()
            if CONF_NAME in user_input and not networks:
                self._set_choice(device, user_input[CONF_ROLE])
                self._target = None  # a stale target must not outlive its network
                self._name = user_input[CONF_NAME]  # the last name submitted, keys kept
                if self._new_network is None:  # a retry keeps the network made for the first try
                    net_key = secrets.token_bytes(16)
                    network_id = NetworkKeys.from_net_key(net_key).network_id.hex()
                    if not await self._async_reserve_address(network_id, NEW_NETWORK_ADDRESS):
                        return self.async_abort(reason="cannot_save")
                    self._new_network = network_data(
                        self._name, net_key, secrets.token_bytes(16), NEW_NETWORK_ADDRESS
                    )
                else:
                    self._new_network[CONF_NAME] = self._name
                return await self.async_step_provision()
        fields: dict[Any, Any] = {}
        if networks:
            options = [SelectOptionDict(value=e.entry_id, label=e.title) for e in networks]
            fields[vol.Required(CONF_NETWORK, default=networks[0].entry_id)] = SelectSelector(
                SelectSelectorConfig(options=options)
            )
        else:
            fields[vol.Required(CONF_NAME, default=self._name)] = str
        fields[vol.Required(CONF_ROLE, default=ROLE_AUTO)] = role_selector()
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={**placeholders, "name": device_title(device)},
        )

    async def _async_provision(self, device: UnprovisionedDevice) -> ProvisioningResult:
        if self._target is not None:
            return await async_provision_into_entry(self.hass, self._target, device)
        assert self._new_network is not None
        return await async_provision_device(
            self.hass, self._new_network, device, iv_index=0, taken=[(NEW_NETWORK_ADDRESS, 1)]
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The device is on the mesh: the network sets it up on its next connect."""
        assert self._result is not None and self._device is not None
        result, title = self._result, device_title(self._device)
        data = provisioned_subentry_data(result, self._role, self._device.address)
        unique_id = node_unique_id(result.unicast)
        if (entry := self._target) is not None:
            forget_replay(entry, result)
            self.hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=SUBENTRY_NODE,
                    title=title,
                    unique_id=unique_id,
                ),
            )
            return self.async_abort(
                reason="device_added",
                description_placeholders={"name": title, "network": entry.title},
            )
        assert self._new_network is not None
        net_key = bytes.fromhex(self._new_network[CONF_NET_KEY])
        await self.async_set_unique_id(
            NetworkKeys.from_net_key(net_key).network_id.hex(), raise_on_progress=False
        )
        return self.async_create_entry(
            title=self._name,
            data=self._new_network,
            options=dict(DEFAULT_OPTIONS),
            subentries=[
                {
                    "subentry_type": SUBENTRY_NODE,
                    "title": title,
                    "unique_id": unique_id,
                    "data": data,
                }
            ],
        )

    async def _async_reserve_address(self, network_id: str, address: int) -> bool:
        """Record ``address`` as ours for good, so no later import or reset reuses its seqs."""
        try:
            await AddressBook(self.hass).async_add(network_id, address)
        except (HomeAssistantError, OSError):
            _LOGGER.exception("Could not save the provisioner address book")
            return False
        return True

    async def _async_reserve_nodes(self, network_id: str, blocks: list[tuple[int, int]]) -> bool:
        """Record the addresses of imported devices, so Add device never gives them out again,
        even after the device is deleted here (a deletion does not reset the device)."""
        try:
            await AddressBook(self.hass).async_add_nodes(network_id, blocks)
        except (HomeAssistantError, OSError):
            _LOGGER.exception("Could not save the provisioner address book")
            return False
        return True

    async def _async_clear_ignored(self, ignored: ConfigEntry | None) -> None:
        """HA skips async_on_create_entry when it replaces an ignored entry, so clear it
        ourselves; an ignored entry holds no state."""
        if ignored is not None:
            with contextlib.suppress(UnknownEntry):  # the user removed it meanwhile
                await self.hass.config_entries.async_remove(ignored.entry_id)

    async def async_step_create(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A new network: fresh random keys, IV index 0, Home Assistant at 0x0001."""
        if user_input is None:
            schema = vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NETWORK_NAME): str})
            return self.async_show_form(step_id="create", data_schema=schema)
        name = user_input[CONF_NAME]
        net_key, app_key = secrets.token_bytes(16), secrets.token_bytes(16)
        network_id = NetworkKeys.from_net_key(net_key).network_id.hex()
        await self.async_set_unique_id(network_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        if not await self._async_reserve_address(network_id, NEW_NETWORK_ADDRESS):
            return self.async_abort(reason="cannot_save")
        return self.async_create_entry(
            title=name,
            data=network_data(name, net_key, app_key, NEW_NETWORK_ADDRESS),
            options=dict(DEFAULT_OPTIONS),
        )

    async def async_step_nrf_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste an nRF Mesh export (Settings > Export > Export everything, as JSON)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result = await self._async_nrf_import(user_input[CONF_NRF_JSON])
            except NrfImportError as err:
                errors["base"] = "no_address" if err.reason == "no_address" else f"nrf_{err.reason}"
            else:
                if not isinstance(result, NrfImport):
                    return result
                return await self._async_create_nrf_entry(result)
        schema = vol.Schema(
            {vol.Required(CONF_NRF_JSON): TextSelector(TextSelectorConfig(multiline=True))}
        )
        return self.async_show_form(step_id="nrf_import", data_schema=schema, errors=errors)

    async def _async_nrf_import(self, text: str) -> NrfImport | ConfigFlowResult:
        probe = import_nrf_json(text)
        network_id = NetworkKeys.from_net_key(probe.net_key).network_id.hex()
        existing, self._ignored = self._network_entry(network_id)
        if existing is not None:
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(network_id, raise_on_progress=False)
        used = await AddressBook(self.hass).async_used(network_id)
        return import_nrf_json(text, used_addresses=used)

    async def _async_create_nrf_entry(self, result: NrfImport) -> ConfigFlowResult:
        network_id = NetworkKeys.from_net_key(result.net_key).network_id.hex()
        if not await self._async_reserve_address(
            network_id, result.address
        ) or not await self._async_reserve_nodes(network_id, result.occupied):
            return self.async_abort(reason="cannot_save")
        await self._async_clear_ignored(self._ignored)
        if result.skipped:
            _LOGGER.warning(
                "%s: %d nodes were not imported (excluded, no device key, or no on/off or "
                "light model)",
                result.title,
                result.skipped,
            )
        return self.async_create_entry(
            title=result.title,
            data=network_data(
                result.title,
                result.net_key,
                result.app_key,
                result.address,
                iv_index=result.iv_index,
                net_key_index=result.net_key_index,
                app_key_index=result.app_key_index,
                range_low=result.range_low,
                range_high=result.range_high,
            ),
            options=dict(DEFAULT_OPTIONS),
            subentries=[nrf_subentry(nrf_node) for nrf_node in result.nodes],
        )

    async def async_step_tuya_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entries = self._tuya_entries()
        errors: dict[str, str] = {}
        if user_input is not None:
            source = self.hass.config_entries.async_get_entry(user_input[CONF_SOURCE_ENTRY])
            if source is None or source not in entries:
                errors["base"] = "not_sig"
            else:
                try:
                    result = await self._async_import(source)
                except TuyaImportError as err:
                    errors["base"] = err.reason
                else:
                    if isinstance(result, dict):
                        return result
                    self._import, self._source = result, source
                    self._name = user_input[CONF_NAME]
                    return await self.async_step_disable_old()
        options = [SelectOptionDict(value=entry.entry_id, label=entry.title) for entry in entries]
        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_ENTRY): SelectSelector(
                    SelectSelectorConfig(options=options)
                ),
                vol.Required(CONF_NAME, default=DEFAULT_NETWORK_NAME): str,
            }
        )
        return self.async_show_form(step_id="tuya_import", data_schema=schema, errors=errors)

    def _network_entry(self, network_id: str) -> tuple[ConfigEntry | None, ConfigEntry | None]:
        """(the network entry we have, an ignored discovery entry for the same ID)."""
        found = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, network_id)
        if found is not None and found.source == SOURCE_IGNORE:
            return None, found  # its data is empty; HA would replace it on create
        return found, None

    @staticmethod
    def _join_conflict(existing: ConfigEntry, node_import: TuyaImport) -> str | None:
        """Why ``node_import`` cannot join ``existing``, as an abort reason."""
        own = int(existing.data[CONF_ADDRESS])
        if node_import.old_address == own:
            # The old integration sent from our address: same sender, same nonces.
            return "address_conflict"
        node = node_import.node
        start, end = node.unicast, node.unicast + node.element_count
        claimed = [(own, 1)]
        for sub in existing.subentries.values():
            if sub.subentry_type == SUBENTRY_NODE and (rng := claimed_range(sub.data)):
                claimed.append(rng)
        if any(start < first + count and first < end for first, count in claimed):
            return "already_configured"
        return None

    async def _async_import(self, source: ConfigEntry) -> TuyaImport | ConfigFlowResult:
        """Import ``source``; joins the network entry we already have for its NetKey."""
        probe = import_tuya_entry(source.title, source.data)
        network_id = NetworkKeys.from_net_key(probe.net_key).network_id.hex()
        existing, ignored = self._network_entry(network_id)
        if existing is not None:
            # Joining keeps the network's own address, so the allocated one is discarded:
            # skip the address book rather than risk a false no_address.
            if (reason := self._join_conflict(existing, probe)) is not None:
                return self.async_abort(reason=reason)
            used: list[tuple[int, int]] = []
        else:
            used = await AddressBook(self.hass).async_used(network_id)
            await self.async_set_unique_id(network_id, raise_on_progress=False)
        self._existing, self._ignored = existing, ignored
        return import_tuya_entry(source.title, source.data, used_addresses=used)

    async def async_step_disable_old(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Two integrations would fight over the bulb's single connection."""
        assert self._import is not None and self._source is not None
        if user_input is None:
            return self.async_show_form(
                step_id="disable_old",
                data_schema=vol.Schema({vol.Optional(CONF_DISABLE_OLD, default=True): bool}),
                description_placeholders={"old": self._source.title},
            )
        node_import = self._import
        self._disable_old = bool(user_input[CONF_DISABLE_OLD])
        unique_id = node_unique_id(node_import.node.unicast)
        network_id = NetworkKeys.from_net_key(node_import.net_key).network_id.hex()
        if self.hass.config_entries.async_get_entry(self._source.entry_id) is None:
            return self.async_abort(reason="no_import_source")
        existing, ignored = self._network_entry(network_id)
        if (existing is None) != (self._existing is None) or (
            existing is not None and existing is not self._existing
        ):
            # The network was added or removed while the form was open.
            return self.async_abort(
                reason="network_gone" if existing is None else "already_configured"
            )
        block = [(node_import.node.unicast, node_import.node.element_count)]
        if existing is not None:
            if (reason := self._join_conflict(existing, node_import)) is not None:
                return self.async_abort(reason=reason)
            if not await self._async_reserve_nodes(network_id, block):
                return self.async_abort(reason="cannot_save")
            self.hass.config_entries.async_add_subentry(
                existing,
                ConfigSubentry(
                    data=MappingProxyType(node_subentry_data(node_import)),
                    subentry_type=SUBENTRY_NODE,
                    title=node_import.title,
                    unique_id=unique_id,
                ),
            )
            await self._async_disable_old()
            return self.async_abort(reason="node_added")
        if not await self._async_reserve_address(
            network_id, node_import.address
        ) or not await self._async_reserve_nodes(network_id, block):
            return self.async_abort(reason="cannot_save")
        await self._async_clear_ignored(ignored)
        return self.async_create_entry(
            title=self._name,
            data=network_data(
                self._name,
                node_import.net_key,
                node_import.app_key,
                node_import.address,
                iv_index=node_import.iv_index,
            ),
            options=dict(DEFAULT_OPTIONS),
            subentries=[
                {
                    "subentry_type": SUBENTRY_NODE,
                    "data": node_subentry_data(node_import),
                    "title": node_import.title,
                    "unique_id": unique_id,
                }
            ],
        )

    async def _async_disable_old(self) -> None:
        """Two integrations would fight over the bulb: disable the old entry, if asked to."""
        if not self._disable_old or self._source is None:
            return
        with contextlib.suppress(UnknownEntry):  # the user removed it meanwhile
            await self.hass.config_entries.async_set_disabled_by(
                self._source.entry_id, ConfigEntryDisabler.USER
            )

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Runs once the network entry exists and is set up: only now is the old entry
        disabled, and then our entities take over its entity ids."""
        await self._async_disable_old()
        source = self._source
        if self._disable_old and source is not None and source.disabled_by is not None:
            take_over_entity_ids(self.hass, source.entry_id, result["result"].entry_id)
        return result


@callback
def take_over_entity_ids(hass: HomeAssistant, source_entry_id: str, entry_id: str) -> None:
    """Give our light (or switch) the entity id the old entry's entity had.

    Dashboards, automations and voice aliases point at that id. The old entity is renamed to
    ``<domain>.<object_id>_tuya_ble_mesh`` (or the next free id), then ours takes the freed
    one. Only one entity per domain on each side is paired; other domains do not clash.
    Best effort: a registry error is logged and the import goes on.
    """
    registry = er.async_get(hass)
    ours: dict[str, list[er.RegistryEntry]] = {}
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.platform == DOMAIN and entity.domain in SWAPPED_DOMAINS:
            ours.setdefault(entity.domain, []).append(entity)
    theirs: dict[str, list[er.RegistryEntry]] = {}
    for entity in er.async_entries_for_config_entry(registry, source_entry_id):
        theirs.setdefault(entity.domain, []).append(entity)
    for domain, mine in ours.items():
        old = theirs.get(domain, [])
        if len(mine) != 1 or len(old) != 1:
            continue  # nothing to swap with, or no telling which goes with which
        new, wanted = mine[0], old[0].entity_id
        state = hass.states.get(wanted)
        if state is not None and not state.attributes.get(ATTR_RESTORED):
            # The old entity is still live (its entry did not unload): the id would not come
            # free, so neither rename could finish and both would be stranded off it.
            _LOGGER.warning(
                "Not giving %s the id %s: the old entity is still loaded", new.entity_id, wanted
            )
            continue
        try:
            moved = registry.async_get_available_entity_id(
                domain, f"{split_entity_id(wanted)[1]}_{TUYA_DOMAIN}"
            )
            registry.async_update_entity(wanted, new_entity_id=moved)
            try:
                registry.async_update_entity(new.entity_id, new_entity_id=wanted)
            except Exception:
                registry.async_update_entity(moved, new_entity_id=wanted)  # put it back
                raise
        except Exception as err:  # the import has worked: never fail it over a name
            _LOGGER.warning("Could not give %s the id %s: %s", new.entity_id, wanted, err)
        else:
            _LOGGER.info("%s is now %s; the old entity is %s", new.entity_id, wanted, moved)


@callback
def give_back_entity_ids(hass: HomeAssistant, entry_id: str) -> None:
    """Undo ``take_over_entity_ids`` as the network is deleted.

    Our entities are unloaded but still registered: each steps aside, then the old entity it
    displaced (``<id>_tuya_ble_mesh`` or ``<id>_tuya_ble_mesh_<n>``, from tuya_ble_mesh)
    gets the id back. HA then deletes ours by config entry, whatever their id. Best effort:
    a registry error is logged and the delete goes on.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.platform != DOMAIN or entity.domain not in SWAPPED_DOMAINS:
            continue
        wanted = entity.entity_id
        aside = re.compile(rf"{re.escape(wanted)}_{TUYA_DOMAIN}(_\d+)?")
        old = [
            e
            for e in registry.entities.values()
            if e.platform == TUYA_DOMAIN and aside.fullmatch(e.entity_id)
        ]
        if len(old) != 1:
            continue  # never swapped, renamed since, or no telling which one it was
        moved = old[0].entity_id
        try:
            registry.async_update_entity(
                wanted,
                new_entity_id=registry.async_get_available_entity_id(
                    entity.domain, f"{split_entity_id(wanted)[1]}_{DOMAIN}"
                ),
            )
            registry.async_update_entity(moved, new_entity_id=wanted)
        except Exception as err:  # the delete goes on: never fail it over a name
            _LOGGER.warning("Could not give %s back the id %s: %s", moved, wanted, err)
        else:
            _LOGGER.info("%s has its id %s back", moved, wanted)


class AoiSigMeshOptionsFlow(OptionsFlow):
    """Saving reloads the network (the update listener sees the options changed)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_IDLE_TIMEOUT: int(user_input[CONF_IDLE_TIMEOUT]),
                    CONF_PUBLISH_SETUP: user_input[CONF_PUBLISH_SETUP],
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_IDLE_TIMEOUT,
                    default=options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=IDLE_TIMEOUT_MAX,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_PUBLISH_SETUP,
                    default=options.get(CONF_PUBLISH_SETUP, DEFAULT_PUBLISH_SETUP),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class NodeSubentryFlow(ProvisionSteps, ConfigSubentryFlow):
    """Add device provisions a factory-reset device; reconfigure (Manage device) changes the
    role, runs the setup again or resets and removes the device."""

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict[str, UnprovisionedDevice] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick a factory-reset device in range, and what it should be."""
        failure = self._failure
        errors, placeholders = self._take_failure()
        if failure is None and user_input is not None:
            # the selector only accepts a listed device
            self._set_choice(self._devices[user_input[CONF_DEVICE]], user_input[CONF_ROLE])
            return await self.async_step_provision()
        devices = async_unprovisioned(self.hass)
        if not devices:
            if failure is not None:  # the real reason, not "nothing in range"
                return self.async_abort(
                    reason=failure.reason,
                    description_placeholders={"step": failure.step, "detail": failure.detail},
                )
            return self.async_abort(reason="no_devices")
        self._devices = {device.address: device for device in devices}
        addresses = [device.address for device in devices]
        # match by address: the signal strength changes between scans
        default = (
            self._device.address
            if self._device and self._device.address in addresses
            else addresses[0]
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=device.address, label=device_label(device))
                            for device in devices
                        ]
                    )
                ),
                vol.Required(CONF_ROLE, default=ROLE_AUTO): role_selector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_provision(self, device: UnprovisionedDevice) -> ProvisioningResult:
        return await async_provision_into_entry(self.hass, self._get_entry(), device)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The device is on the mesh: the network sets it up on its next connect."""
        assert self._result is not None and self._device is not None
        result = self._result
        forget_replay(self._get_entry(), result)
        return self.async_create_entry(
            title=device_title(self._device),
            data=provisioned_subentry_data(result, self._role, self._device.address),
            unique_id=node_unique_id(result.unicast),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["role", "setup_again", "reset_remove"]
        )

    async def async_step_role(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Change the device type (light or switch)."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={CONF_ROLE: user_input[CONF_ROLE]},
            )
        # A stored role that is not light or switch (damaged data) must not become the default:
        # read the node with the role cleared, so the default is what the node is.
        role = node_role(subentry.data, read_node({**subentry.data, CONF_ROLE: None}))
        schema = vol.Schema(
            {
                vol.Required(CONF_ROLE, default=role): SelectSelector(
                    SelectSelectorConfig(
                        options=[ROLE_LIGHT, ROLE_SWITCH], translation_key=CONF_ROLE
                    )
                ),
            }
        )
        return self.async_show_form(step_id="role", data_schema=schema)

    def _loaded_node(self) -> str | None:
        """The subentry ID, if the network is running and could read this node."""
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        if (
            entry.state is ConfigEntryState.LOADED
            and subentry.subentry_id in entry.runtime_data.nodes
        ):
            return subentry.subentry_id
        return None

    async def async_step_setup_again(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Set the node up on the mesh again, as on the first connect; the role stays."""
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        if user_input is None:
            return self.async_show_form(
                step_id="setup_again", description_placeholders={"name": subentry.title}
            )
        self.hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, CONF_CONFIGURED: False}
        )
        if (subentry_id := self._loaded_node()) is not None:
            entry.runtime_data.async_setup_again(subentry_id)
        return self.async_abort(
            reason="setup_again_started", description_placeholders={"name": subentry.title}
        )

    async def async_step_reset_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Send Config Node Reset, then delete the device here. Deleting a device any other way
        leaves it on the mesh with the network's keys (its address is never given out again)."""
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        placeholders = {"name": subentry.title}
        errors: dict[str, str] = {}
        if user_input is not None:
            confirmed = False
            if (subentry_id := self._loaded_node()) is not None:
                try:
                    await entry.runtime_data.async_reset_node(subentry_id)
                except HomeAssistantError as err:
                    _LOGGER.warning("%s: Node Reset was not confirmed: %s", subentry.title, err)
                else:
                    confirmed = True
            if confirmed or user_input[CONF_REMOVE_ANYWAY]:
                self.hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
                return self.async_abort(
                    reason="device_reset" if confirmed else "device_removed",
                    description_placeholders=placeholders,
                )
            errors["base"] = "reset_failed"
        schema = vol.Schema({vol.Required(CONF_REMOVE_ANYWAY, default=False): bool})
        return self.async_show_form(
            step_id="reset_remove",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )
