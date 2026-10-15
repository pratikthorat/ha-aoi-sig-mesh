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
        "## Disclaimer",
    ):
        assert heading in README, heading


def test_readme_says_it_is_independent() -> None:
    assert "not affiliated with" in README
    assert "Bluetooth® is a registered trademark of Bluetooth SIG, Inc." in README


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
PRIVATE_IP = re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
MAC = re.compile(r"(?i)\b[0-9a-f]{2}([:-])[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}\b")
TEST_MACS = ("AA:", "DC:23:50:00:00:")  # made up; a real device's MAC is a house detail


def test_no_forbidden_files_or_house_details() -> None:
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts) or not path.is_file():
            continue
        assert path.name not in FORBIDDEN_NAMES, path
        assert not path.name.endswith((".pem", ".key")), path
        if path.suffix in {".md", ".json", ".yaml", ".yml", ".toml", ".py", ".txt"}:
            text = path.read_text(errors="ignore")
            assert not PRIVATE_IP.search(text), path
            for mac in MAC.finditer(text):
                assert mac[0].upper().replace("-", ":").startswith(TEST_MACS), (path, mac[0])
