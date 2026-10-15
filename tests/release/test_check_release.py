"""The release check that keeps the tag, the manifest version and the changelog in step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.check_release import release_problems


@pytest.fixture
def root(tmp_path: Path) -> Path:
    integration = tmp_path / "custom_components" / "aoi_sig_mesh"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(json.dumps({"version": "0.2.0"}))
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0\n\n- Things.\n\n## 0.1.0\n")
    return tmp_path


def test_a_matching_tag_passes(root: Path) -> None:
    assert release_problems("v0.2.0", root) == []


@pytest.mark.parametrize("tag", ["0.2.0", "v0.1.0", "v0.2.0-beta"])
def test_a_tag_that_is_not_v_plus_the_manifest_version_fails(root: Path, tag: str) -> None:
    (problem,) = release_problems(tag, root)
    assert "want v0.2.0" in problem


def test_a_version_without_a_changelog_entry_fails(root: Path) -> None:
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n")
    assert release_problems("v0.2.0", root) == ["CHANGELOG.md has no '## 0.2.0' entry"]


def test_a_longer_version_does_not_count_as_the_entry(root: Path) -> None:
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0.1\n")
    assert release_problems("v0.2.0", root) == ["CHANGELOG.md has no '## 0.2.0' entry"]


def test_the_real_repo_is_ready_to_tag_its_manifest_version() -> None:
    root = Path(__file__).resolve().parents[2]
    version = json.loads((root / "custom_components/aoi_sig_mesh/manifest.json").read_text())
    assert release_problems(f"v{version['version']}") == []
