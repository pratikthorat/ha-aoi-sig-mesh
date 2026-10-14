"""The mesh package must stay free of Home Assistant and bleak."""

import re
from pathlib import Path

MESH = Path(__file__).resolve().parents[2] / "custom_components" / "aoi_sig_mesh" / "mesh"
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(homeassistant|bleak)\b", re.MULTILINE)


def test_mesh_package_exists():
    assert (MESH / "__init__.py").is_file()


def test_mesh_has_no_homeassistant_or_bleak_imports():
    offenders = [p.name for p in MESH.glob("*.py") if FORBIDDEN.search(p.read_text())]
    assert offenders == []
