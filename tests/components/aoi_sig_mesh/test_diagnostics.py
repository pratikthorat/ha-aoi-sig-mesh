"""Diagnostics never contain key material."""

from __future__ import annotations

import base64
import json
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.aoi_sig_mesh.const import CONF_APP_KEY, CONF_NET_KEY
from custom_components.aoi_sig_mesh.diagnostics import async_get_config_entry_diagnostics
from tests.sim.mesh_sim import APP_KEY, NET_KEY, SimNode
from tests.sim.sim_radio import BULB_MAC, SimRadio

from .conftest import until

REDACTED = "**REDACTED**"


def _assert_no_keys(result: dict[str, Any], bulb: SimNode) -> None:
    """Scans the whole result: no key in any form, and not the bulb's MAC anywhere."""
    dumped = json.dumps(result, default=str)
    for key in (NET_KEY, APP_KEY, bulb.dev_key):
        assert key.hex() not in dumped
        assert key.hex().upper() not in dumped
        assert json.dumps(list(key))[1:-1] not in dumped  # as a list of byte values
        assert base64.b64encode(key).decode() not in dumped
    for mac in (BULB_MAC, BULB_MAC.replace(":", "")):
        assert mac.lower() not in dumped.lower()  # a device identifier (HA convention)


async def test_diagnostics_redact_every_key(
    hass: HomeAssistant, bulb: SimNode, bulb_entry: Any
) -> None:
    bulb_entry.runtime_data.network.accept_replay(bulb.unicast, 0, 0)  # an old seq: dropped
    result = await async_get_config_entry_diagnostics(hass, bulb_entry)

    _assert_no_keys(result, bulb)
    # Redacted in place, so a wrong key name cannot pass just because the key is absent.
    assert result["entry"]["data"][CONF_NET_KEY] == REDACTED
    assert result["entry"]["data"][CONF_APP_KEY] == REDACTED
    subentry = result["entry"]["subentries"][0]
    assert subentry["data"]["dev_key"] == REDACTED
    assert subentry["data"]["mac"] == REDACTED
    assert result["connection"]["connected"] is True
    assert result["connection"]["proxy"] == REDACTED  # the bulb is the proxy here
    assert result["nodes"]["Desk lamp"]["unicast"] == "0x00B0"
    assert result["network"]["state"]["seq"] > 0
    assert result["network"]["replay_drops"] == {"0x00B0": 1}


async def test_diagnostics_while_disconnected(
    hass: HomeAssistant, radio: SimRadio, bulb: SimNode, bulb_entry: Any
) -> None:
    coordinator = bulb_entry.runtime_data
    radio.forget()  # nothing to reconnect to
    radio.transports[-1].drop()
    await until(lambda: not coordinator.connected)

    result = await async_get_config_entry_diagnostics(hass, bulb_entry)

    _assert_no_keys(result, bulb)
    assert result["connection"]["connected"] is False
    assert result["connection"]["proxy"] is None
    assert result["entry"]["data"][CONF_NET_KEY] == REDACTED
    assert "Desk lamp" in result["nodes"]
