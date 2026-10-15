"""Home Assistant test harness for aoi_sig_mesh.

The simulator imports the mesh library as top-level ``mesh``; here that name is aliased to
``custom_components.aoi_sig_mesh.mesh`` so both sides share one set of classes.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import custom_components.aoi_sig_mesh.mesh as _mesh  # noqa: E402

sys.modules["mesh"] = _mesh
for _info in pkgutil.iter_modules(_mesh.__path__):
    sys.modules[f"mesh.{_info.name}"] = importlib.import_module(
        f"custom_components.aoi_sig_mesh.mesh.{_info.name}"
    )

from homeassistant.core import HomeAssistant  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None, hass: HomeAssistant) -> None:
    """Load custom_components/, and count bluetooth as set up (it cannot start on macOS)."""
    hass.config.components.add("bluetooth")
