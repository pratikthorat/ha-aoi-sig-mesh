"""Config flow: one entry per mesh network, one "node" subentry per device."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.selector import (
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
    SUBENTRY_NODE,
    TUYA_DOMAIN,
)
from .coordinator import default_role
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


def node_subentry_data(node_import: TuyaImport) -> dict[str, Any]:
    node = node_import.node
    return {
        **node.to_dict(),
        CONF_ROLE: default_role(node),
        CONF_MAC: node_import.mac,
        CONF_CONFIGURED: False,
    }


# How long a pending "disable the old entry" waits for the new entry to be created.
DISABLE_OLD_WAIT = 300.0


def disable_when_created(hass: HomeAssistant, source_id: str, network_id: str) -> None:
    """Disable ``source_id`` once the entry for ``network_id`` exists, not before.

    A flow cannot run code after it returns ``create_entry``, so this waits for the entry
    to be added. If creation fails the old entry is left alone.
    """
    done = False

    @callback
    def cleanup() -> None:
        nonlocal done
        if not done:
            done = True
            unsub()
            timer.cancel()

    @callback
    def changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if (
            change is ConfigEntryChange.ADDED
            and entry.domain == DOMAIN
            and entry.unique_id == network_id
        ):
            cleanup()
            hass.async_create_task(
                hass.config_entries.async_set_disabled_by(source_id, ConfigEntryDisabler.USER),
                f"{DOMAIN} disable old entry",
            )

    unsub = async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed)
    timer = hass.loop.call_later(DISABLE_OLD_WAIT, cleanup)


def node_unique_id(unicast: int) -> str:
    return f"{unicast:04x}"


class AoiSigMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._import: TuyaImport | None = None
        self._source: ConfigEntry | None = None
        self._existing: ConfigEntry | None = None
        self._name = DEFAULT_NETWORK_NAME

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

    async def _async_import(self, source: ConfigEntry) -> TuyaImport | ConfigFlowResult:
        """Import ``source``; joins the network entry we already have for its NetKey."""
        probe = import_tuya_entry(source.title, source.data)
        network_id = NetworkKeys.from_net_key(probe.net_key).network_id.hex()
        used = await AddressBook(self.hass).async_used(network_id)
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, network_id)
        if existing is not None:
            nodes = [
                (int(sub.data["unicast"]), int(sub.data["element_count"]))
                for sub in existing.subentries.values()
            ]
            if any(start <= probe.node.unicast < start + count for start, count in nodes):
                return self.async_abort(reason="already_configured")
            used = [*used, (int(existing.data[CONF_ADDRESS]), 1), *nodes]
        else:
            await self.async_set_unique_id(network_id, raise_on_progress=False)
        self._existing = existing
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
        unique_id = node_unique_id(node_import.node.unicast)
        if self._existing is not None:
            self.hass.config_entries.async_add_subentry(
                self._existing,
                ConfigSubentry(
                    data=MappingProxyType(node_subentry_data(node_import)),
                    subentry_type=SUBENTRY_NODE,
                    title=node_import.title,
                    unique_id=unique_id,
                ),
            )
            if user_input[CONF_DISABLE_OLD]:
                await self.hass.config_entries.async_set_disabled_by(
                    self._source.entry_id, ConfigEntryDisabler.USER
                )
            return self.async_abort(reason="node_added")
        network_id = NetworkKeys.from_net_key(node_import.net_key).network_id.hex()
        await AddressBook(self.hass).async_add(network_id, node_import.address)
        if user_input[CONF_DISABLE_OLD]:
            disable_when_created(self.hass, self._source.entry_id, network_id)
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
