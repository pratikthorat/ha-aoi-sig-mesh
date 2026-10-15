"""Check that a release tag matches the manifest version and has a changelog entry.

The Release workflow runs
    python tools/check_release.py v0.1.0
when a GitHub release is published. HACS shows users the tag, and Home Assistant shows the
manifest version, so the two must agree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def release_problems(tag: str, root: Path = ROOT) -> list[str]:
    manifest = root / "custom_components" / "aoi_sig_mesh" / "manifest.json"
    version = json.loads(manifest.read_text())["version"]
    problems = []
    if tag != f"v{version}":
        problems.append(
            f"tag {tag} does not match the manifest version {version} (want v{version})"
        )
    if f"## {version}\n" not in (root / "CHANGELOG.md").read_text():
        problems.append(f"CHANGELOG.md has no '## {version}' entry")
    return problems


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_release.py <tag>")
    problems = release_problems(sys.argv[1])
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"{sys.argv[1]} matches the manifest and the changelog")


if __name__ == "__main__":
    main()
