"""Release metadata: the manifest rules hassfest enforces for custom integrations, and hacs.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "custom_components" / "aoi_sig_mesh"
REPO_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/ha-aoi-sig-mesh")
# The logo, the release badge and the Open-in-HACS button name the owner in other shapes.
OTHER_pratikthorat_RE = re.compile(
    r"(?:raw\.githubusercontent\.com/|github/v/release/|owner=)([A-Za-z0-9-]+)"
)
IOT_CLASSES = {
    "assumed_state",
    "calculated",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
}
INTEGRATION_TYPES = {
    "device",
    "entity",
    "hardware",
    "helper",
    "hub",
    "service",
    "system",
    "virtual",
}


def manifest() -> dict:
    return json.loads((INTEGRATION / "manifest.json").read_text())


def test_manifest_keys_are_hassfest_ordered() -> None:
    keys = list(manifest())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_manifest_has_the_custom_integration_fields() -> None:
    data = manifest()
    assert data["domain"] == "aoi_sig_mesh"
    assert data["name"] == "AoI Bluetooth SIG Mesh"
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"])
    assert data["iot_class"] in IOT_CLASSES
    assert data["integration_type"] in INTEGRATION_TYPES
    assert data["config_flow"] is True
    assert data["requirements"] == []
    assert data["dependencies"] == ["bluetooth"]
    assert data["documentation"].startswith("https://github.com/")
    assert data["issue_tracker"] == data["documentation"] + "/issues"
    assert len(data["codeowners"]) == 1
    assert data["codeowners"][0].startswith("@")


def test_every_owner_link_agrees() -> None:
    data = manifest()
    owner = REPO_RE.fullmatch(data["documentation"]).group(1)
    assert data["codeowners"] == [f"@{owner}"]
    for name in ("README.md", "RELEASING.md", "SECURITY.md", ".github/ISSUE_TEMPLATE/config.yml"):
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text()
        found = {*REPO_RE.findall(text), *OTHER_pratikthorat_RE.findall(text)}
        assert found <= {owner}, f"{name} links to {found - {owner}}"


def test_hacs_json() -> None:
    data = json.loads((ROOT / "hacs.json").read_text())
    assert data == {
        "name": "AoI Bluetooth SIG Mesh",
        "homeassistant": "2026.5.0",
        "render_readme": True,
    }


def test_licence_is_plain_mit_with_both_copyrights() -> None:
    # GitHub, and so HACS, only recognises the licence when nothing but
    # copyright lines differs from the MIT text. The credit lives in NOTICE.
    lines = (ROOT / "LICENSE").read_text().splitlines()
    assert lines[:4] == [
        "MIT License",
        "",
        "Copyright (c) 2026 Pratik Thorat (Arena of Innovation)",
        "Copyright (c) 11z4t and contributors",
    ]
    assert lines[4] == ""
    assert lines[5].startswith("Permission is hereby granted, free of charge")
    assert not [line for line in lines[5:] if "Copyright" in line or "http" in line]


def test_notice_keeps_the_credit() -> None:
    text = (ROOT / "NOTICE").read_text()
    assert "https://github.com/11z4t/tuya-ble-mesh" in text
    assert "MIT License" in text
