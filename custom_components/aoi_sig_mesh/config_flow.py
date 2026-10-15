"""Config flow: one entry per mesh network, one "node" subentry per device."""

from __future__ import annotations

import contextlib
import logging
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
    UnknownEntry,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    CONF_ADDRESS,
    CONF_APP_KEY,
    CONF_APP_KEY_INDEX,
    CONF_CONFIGURED,
    CONF_DISABLE_OLD,
    CONF_IDLE_TIMEOUT,
    CONF_IV_INDEX,
    CONF_MAC,
    CONF_NAME,
    CONF_NET_KEY,
    CONF_NET_KEY_INDEX,
    CONF_PUBLISH_SETUP,
    CONF_RANGE_HIGH,
    CONF_RANGE_LOW,
    CONF_ROLE,
    CONF_SOURCE_ENTRY,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_NETWORK_NAME,
    DEFAULT_PUBLISH_SETUP,
    DOMAIN,
    PROXY_SERVICE_UUID,
    ROLE_LIGHT,
    ROLE_SWITCH,
    SUBENTRY_NODE,
    TUYA_DOMAIN,
)
from .coordinator import claimed_range, default_role, node_role, read_node
from .mesh.importers import (
    NODE_RANGE_HIGH,
    NODE_RANGE_LOW,
    TuyaImport,
    TuyaImportError,
    import_tuya_entry,
    is_tuya_sig_entry,
)
from .mesh.pdu import NetworkKeys
from .mesh.proxy import parse_proxy_service_data
from .store import AddressBook

IDLE_TIMEOUT_MAX = 240  # minutes

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


class AoiSigMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
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
        """A mesh proxy advert. Only a network we can import (and do not have) is offered."""
        data = discovery_info.service_data.get(PROXY_SERVICE_UUID)
        advert = parse_proxy_service_data(data) if data else None
        if advert is None or advert.network_id is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(advert.network_id.hex())
        self._abort_if_unique_id_configured()
        if not self._tuya_entries():
            return self.async_abort(reason="not_supported")
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._tuya_entries():
            return self.async_abort(reason="no_import_source")
        return self.async_show_menu(step_id="user", menu_options=["tuya_import"])

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
        if existing is not None:
            if (reason := self._join_conflict(existing, node_import)) is not None:
                return self.async_abort(reason=reason)
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
        try:
            await AddressBook(self.hass).async_add(network_id, node_import.address)
        except (HomeAssistantError, OSError):
            _LOGGER.exception("Could not save the provisioner address book")
            return self.async_abort(reason="cannot_save")
        if ignored is not None:
            # HA skips async_on_create_entry when it replaces an ignored entry, so clear it
            # ourselves; an ignored entry holds no state.
            with contextlib.suppress(UnknownEntry):  # the user removed it meanwhile
                await self.hass.config_entries.async_remove(ignored.entry_id)
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_NAME: self._name,
                CONF_NET_KEY: node_import.net_key.hex(),
                CONF_NET_KEY_INDEX: 0,
                CONF_APP_KEY: node_import.app_key.hex(),
                CONF_APP_KEY_INDEX: 0,
                CONF_ADDRESS: node_import.address,
                CONF_RANGE_LOW: NODE_RANGE_LOW,
                CONF_RANGE_HIGH: NODE_RANGE_HIGH,
                CONF_IV_INDEX: node_import.iv_index,
            },
            options={
                CONF_IDLE_TIMEOUT: DEFAULT_IDLE_TIMEOUT,
                CONF_PUBLISH_SETUP: DEFAULT_PUBLISH_SETUP,
            },
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
        """Runs once the network entry exists: only now is the old entry disabled."""
        await self._async_disable_old()
        return result


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


class NodeSubentryFlow(ConfigSubentryFlow):
    """Nodes come from imports for now; reconfigure picks the role (light or switch)."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self.async_abort(reason="add_by_import")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={CONF_ROLE: user_input[CONF_ROLE]},
            )
        # a stored role that is not light or switch (damaged data) must not become the default
        role = node_role(subentry.data, read_node(subentry.data))
        schema = vol.Schema(
            {
                vol.Required(CONF_ROLE, default=role): SelectSelector(
                    SelectSelectorConfig(
                        options=[ROLE_LIGHT, ROLE_SWITCH], translation_key=CONF_ROLE
                    )
                ),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)
