"""The README tells a stranger what they need, and credits the code it builds on."""

from __future__ import annotations

import json
import re

from tests.release.test_metadata import ROOT

README = (ROOT / "README.md").read_text() if (ROOT / "README.md").exists() else ""


def test_readme_credits_tuya_ble_mesh() -> None:
    assert "https://github.com/11z4t/tuya-ble-mesh" in README


def test_readme_covers_every_setup_path() -> None:
    for heading in (
        "## Install",
        "## Create a network",
        "## Add a device",
        "## Import from nRF Mesh",
        "## Move from Tuya BLE Mesh",
        "## Manage a device",
        "## Troubleshooting",
        "## What is tested on hardware",
        "## Credits",
    ):
        assert heading in README, heading


def test_readme_says_actions_not_services() -> None:
    text = README.replace("Devices & services", "")  # HA's own menu name
    assert not re.search(r"\bservices?\b", text, flags=re.IGNORECASE)


def test_changelog_has_the_manifest_version() -> None:
    version = json.loads(
        (ROOT / "custom_components" / "aoi_sig_mesh" / "manifest.json").read_text()
    )["version"]
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text()


FORBIDDEN_NAMES = {
    "secrets.yaml",
    "core.config_entries",
    "uc_ro_credentials.json",
    "openrouterkeybackup.txt",
}
SKIP_DIRS = {".git", ".pytest_cache", ".ruff_cache", "__pycache__", ".venv", ".superpowers"}
HOUSE_DETAILS = re.compile(r"192\.168\.\d+\.\d+")


def test_no_forbidden_files_or_house_details() -> None:
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts) or not path.is_file():
            continue
        assert path.name not in FORBIDDEN_NAMES, path
        assert not path.name.endswith((".pem", ".key")), path
        if path.suffix in {".md", ".json", ".yaml", ".yml", ".toml", ".py", ".txt"}:
            assert not HOUSE_DETAILS.search(path.read_text(errors="ignore")), path
