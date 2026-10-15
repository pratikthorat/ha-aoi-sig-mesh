"""Config flow. The import steps arrive in Task 18; HA needs the module from the start."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class AoiSigMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
