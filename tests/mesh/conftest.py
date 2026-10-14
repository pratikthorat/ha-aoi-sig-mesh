"""Make the mesh library importable as top-level ``mesh``."""

import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "aoi_sig_mesh"
if str(COMPONENT) not in sys.path:
    sys.path.insert(0, str(COMPONENT))
