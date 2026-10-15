"""Print the Bluetooth library pins of the installed Home Assistant, one per line.

`pip install homeassistant==X` can resolve newer Bluetooth libraries than X pins, so CI runs
    pip install $(python tools/ci_ha_pins.py)
after installing HA, and tests against the versions a real install of X runs.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

PIN = re.compile(r"^[A-Za-z0-9._-]+==[A-Za-z0-9.+-]+$")


def bluetooth_pins(manifest_path: Path) -> list[str]:
    requirements = json.loads(manifest_path.read_text())["requirements"]
    for requirement in requirements:
        if not PIN.match(requirement):
            raise ValueError(f"not an exact pin: {requirement}")
    return list(requirements)


def main() -> None:
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or spec.origin is None:
        raise SystemExit("homeassistant is not installed")
    manifest = Path(spec.origin).parent / "components" / "bluetooth" / "manifest.json"
    print("\n".join(bluetooth_pins(manifest)))


if __name__ == "__main__":
    main()
