"""CI installs the HA version it pins, with that HA's own Bluetooth library pins."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from tests.release.test_metadata import ROOT
from tools.ci_ha_pins import bluetooth_pins

WORKFLOWS = ROOT / ".github" / "workflows"


def test_pins_come_from_the_bluetooth_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"domain": "bluetooth", "requirements": ["bleak==2.1.1", "habluetooth==6.1.0"]})
    )
    assert bluetooth_pins(manifest) == ["bleak==2.1.1", "habluetooth==6.1.0"]


def test_unpinned_requirements_are_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"requirements": ["bleak>=2"]}))
    with pytest.raises(ValueError, match="bleak>=2"):
        bluetooth_pins(manifest)


def test_ci_installs_the_pinned_ha_and_its_bluetooth_pins() -> None:
    text = (WORKFLOWS / "tests.yml").read_text()
    ha = re.search(r"homeassistant==(\d+\.\d+)\.\d+", text)
    assert ha is not None
    hacs = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    assert hacs.startswith(ha.group(1) + "."), "hacs.json minimum and CI's HA disagree"
    assert "tools/ci_ha_pins.py" in text


def steps(name: str) -> list[dict]:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text())
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_validate_runs_hassfest_and_hacs() -> None:
    uses = [step.get("uses", "") for step in steps("validate.yml")]
    assert any(u.startswith("home-assistant/actions/hassfest@") for u in uses)
    assert any(u.startswith("hacs/action@") for u in uses)


def test_the_hacs_check_ignores_nothing() -> None:
    # The HACS default list refuses a repo whose HACS action ignores any check.
    (hacs,) = [step for step in steps("validate.yml") if step.get("uses", "").startswith("hacs/")]
    assert hacs["with"] == {"category": "integration"}


def test_publishing_a_release_checks_its_tag() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    # PyYAML reads the bare key `on` as True.
    assert workflow[True] == {"release": {"types": ["published"]}}
    assert any("tools/check_release.py" in step.get("run", "") for step in steps("release.yml"))
