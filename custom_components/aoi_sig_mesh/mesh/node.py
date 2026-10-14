"""MeshNode: per-node bookkeeping, model lookup and unicast allocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import CompositionData
from .models import (
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_HEALTH_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_CTL_TEMP_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
)

ROLE_ONOFF = "onoff"
ROLE_LIGHTNESS = "lightness"
ROLE_CTL = "ctl"
ROLE_CTL_TEMP = "ctl_temp"
ROLE_HEALTH = "health"

MODEL_ROLES = {
    MODEL_HEALTH_SERVER: ROLE_HEALTH,
    MODEL_GENERIC_ONOFF_SERVER: ROLE_ONOFF,
    MODEL_LIGHT_LIGHTNESS_SERVER: ROLE_LIGHTNESS,
    MODEL_LIGHT_CTL_SERVER: ROLE_CTL,
    MODEL_LIGHT_CTL_TEMP_SERVER: ROLE_CTL_TEMP,
}

KELVIN_DEFAULT_MIN = 2700
KELVIN_DEFAULT_MAX = 6500
UNICAST_MAX = 0x7FFF


def locate_models(comp: CompositionData, unicast: int) -> dict[str, int]:
    """Map each known model role to the address of the first element hosting it."""
    found: dict[str, int] = {}
    for index, element in enumerate(comp.elements):
        for model in element.sig_models:
            role = MODEL_ROLES.get(model)
            if role is not None and role not in found:
                found[role] = unicast + index
    return found


@dataclass
class MeshNode:
    """A provisioned node: its addresses, DevKey and the models we can drive."""

    unicast: int
    element_count: int
    dev_key: bytes = field(repr=False)
    cid: int | None = None
    pid: int | None = None
    vid: int | None = None
    models: dict[str, int] = field(default_factory=dict)
    kelvin_min: int = KELVIN_DEFAULT_MIN
    kelvin_max: int = KELVIN_DEFAULT_MAX

    def __post_init__(self) -> None:
        if len(self.dev_key) != 16:
            raise ValueError("DevKey must be 16 bytes")
        self._check_range(self.element_count)

    def _check_range(self, element_count: int) -> None:
        if element_count < 1 or self.unicast < 1 or self.unicast + element_count - 1 > UNICAST_MAX:
            raise ValueError(
                f"elements 0x{self.unicast:04X} + {element_count} leave the unicast range"
            )

    def contains(self, address: int) -> bool:
        return self.unicast <= address < self.unicast + self.element_count

    @property
    def is_light(self) -> bool:
        return ROLE_LIGHTNESS in self.models or ROLE_CTL in self.models

    @property
    def is_tunable(self) -> bool:
        return ROLE_CTL in self.models and ROLE_CTL_TEMP in self.models

    def apply_composition(self, comp: CompositionData) -> None:
        self._check_range(len(comp.elements))
        self.element_count = len(comp.elements)
        self.cid, self.pid, self.vid = comp.cid, comp.pid, comp.vid
        self.models = locate_models(comp, self.unicast)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unicast": self.unicast,
            "element_count": self.element_count,
            "dev_key": self.dev_key.hex(),
            "cid": self.cid,
            "pid": self.pid,
            "vid": self.vid,
            "models": dict(self.models),
            "kelvin_min": self.kelvin_min,
            "kelvin_max": self.kelvin_max,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MeshNode:
        return cls(
            unicast=int(data["unicast"]),
            element_count=int(data["element_count"]),
            dev_key=bytes.fromhex(data["dev_key"]),
            cid=data.get("cid"),
            pid=data.get("pid"),
            vid=data.get("vid"),
            models={str(role): int(address) for role, address in data.get("models", {}).items()},
            kelvin_min=int(data.get("kelvin_min", KELVIN_DEFAULT_MIN)),
            kelvin_max=int(data.get("kelvin_max", KELVIN_DEFAULT_MAX)),
        )
