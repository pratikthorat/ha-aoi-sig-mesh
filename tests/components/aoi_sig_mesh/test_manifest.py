"""Home Assistant reads the manifest: name, Bluetooth dependency, config flow."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.aoi_sig_mesh.const import DOMAIN


async def test_home_assistant_loads_the_integration(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.name == "AoI Bluetooth SIG Mesh"
    assert integration.dependencies == ["bluetooth"]
    assert integration.config_flow
