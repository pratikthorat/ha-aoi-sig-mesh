"""Constants for the AoI Bluetooth SIG Mesh integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "aoi_sig_mesh"
MANUFACTURER: Final = "Arena of Innovation"

# entry.data
CONF_NAME: Final = "name"
CONF_NET_KEY: Final = "net_key"
CONF_NET_KEY_INDEX: Final = "net_key_index"
CONF_APP_KEY: Final = "app_key"
CONF_APP_KEY_INDEX: Final = "app_key_index"
CONF_ADDRESS: Final = "address"  # our provisioner unicast
CONF_RANGE_LOW: Final = "range_low"
CONF_RANGE_HIGH: Final = "range_high"
CONF_IV_INDEX: Final = "iv_index"  # IV index at import; the Store wins once it exists

# entry.options
CONF_IDLE_TIMEOUT: Final = "idle_timeout"  # minutes; 0 = stay connected
CONF_PUBLISH_SETUP: Final = "publish_setup"
DEFAULT_IDLE_TIMEOUT: Final = 0
DEFAULT_PUBLISH_SETUP: Final = True

# node subentries: MeshNode.to_dict() keys plus these
SUBENTRY_NODE: Final = "node"
CONF_ROLE: Final = "role"
CONF_MAC: Final = "mac"
CONF_CONFIGURED: Final = "configured"
ROLE_LIGHT: Final = "light"
ROLE_SWITCH: Final = "switch"
ROLE_AUTO: Final = "auto"  # Add device: light if the node has Lightness, else switch

# config flow
CONF_SOURCE_ENTRY: Final = "source_entry"
CONF_DISABLE_OLD: Final = "disable_old"
CONF_NRF_JSON: Final = "nrf_json"
CONF_DEVICE: Final = "device"
CONF_NETWORK: Final = "network"  # discovery: the network entry a device joins
NEW_NETWORK_ADDRESS: Final = 0x0001  # Home Assistant's unicast in a network it creates
TUYA_DOMAIN: Final = "tuya_ble_mesh"
DEFAULT_NETWORK_NAME: Final = "Home mesh"

PROXY_SERVICE_UUID: Final = "00001828-0000-1000-8000-00805f9b34fb"
PROVISIONING_SERVICE_UUID: Final = "00001827-0000-1000-8000-00805f9b34fb"

# repairs
ISSUE_NO_PROXY: Final = "no_proxy"
ISSUE_KEYS_REJECTED: Final = "keys_rejected"
ISSUE_IV_INDEX_JUMP: Final = "iv_index_jump"
ISSUE_NODE_UNREADABLE: Final = "node_unreadable"

# Company identifiers (Bluetooth SIG assigned numbers) shown as the manufacturer.
MANUFACTURERS: Final[dict[int, str]] = {
    0x0059: "Nordic Semiconductor",
    0x005D: "Realtek",
    0x0211: "Telink",
    0x02E5: "Espressif",
    0x02FF: "Silicon Labs",
    0x038F: "Xiaomi",
    0x07D0: "Tuya",
}
