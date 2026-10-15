"""Diagnostics for a mesh network and its nodes; key material is redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from . import AoiConfigEntry
from .const import CONF_APP_KEY, CONF_MAC, CONF_NET_KEY

# "dev_key" is MeshNode.to_dict()'s key in every node subentry. A MAC is no secret, but it
# identifies the device, and HA diagnostics redact those.
TO_REDACT = {CONF_NET_KEY, CONF_APP_KEY, "dev_key", CONF_MAC}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AoiConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    network = coordinator.network
    proxy = coordinator.manager.proxy
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "network": {
            "network_id": coordinator.network_id,
            "address": f"0x{network.address:04X}",
            "state": network.persistent_state(),  # IV index, seq ceiling, replay cache: no keys
            "replay_drops": {f"0x{src:04X}": count for src, count in network.replay_drops.items()},
        },
        "connection": {
            "connected": coordinator.connected,
            "idle": coordinator.manager.idle,
            "proxy": None if proxy is None else REDACTED,  # the proxy is a node: its MAC too
            "rssi": coordinator.rssi,
            "rssi_age": coordinator.rssi_age,
        },
        # Built field by field: never the MeshNode, which holds the DevKey.
        "nodes": {
            record.title: {
                "unicast": f"0x{record.node.unicast:04X}",
                "role": record.role,
                "configured": record.configured,
                "quirks": str(record.quirks),
                "available": record.available,
                "failures": record.failures,
                "last_seen": None if record.last_seen is None else record.last_seen.isoformat(),
                "state": {
                    "on": record.state.on,
                    "lightness": record.state.lightness,
                    "kelvin": record.state.kelvin,
                },
            }
            for record in coordinator.nodes.values()
        },
    }
