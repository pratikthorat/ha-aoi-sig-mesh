"""Lights: dimmable (Light Lightness) and tunable white (Light CTL)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AoiConfigEntry
from .const import ROLE_LIGHT
from .coordinator import MeshCoordinator, NodeRecord
from .entity import AoiNodeEntity
from .mesh.node import ROLE_LIGHTNESS

LIGHTNESS_MAX = 0xFFFF


def brightness_to_lightness(brightness: int) -> int:
    return max(0, min(255, brightness)) * 257  # 255 -> 65535


def lightness_to_brightness(lightness: int) -> int:
    return max(1, round(lightness * 255 / LIGHTNESS_MAX))


def transition_ms(kwargs: dict[str, Any]) -> int | None:
    if (seconds := kwargs.get(ATTR_TRANSITION)) is None:
        return None
    return int(float(seconds) * 1000)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AoiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for record in coordinator.nodes.values():
        if record.role == ROLE_LIGHT:
            async_add_entities(
                [AoiLight(coordinator, record)], config_subentry_id=record.subentry_id
            )


class AoiLight(AoiNodeEntity, LightEntity):
    _attr_name = None
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord) -> None:
        super().__init__(coordinator, record, "light")
        node = record.node
        if node.is_tunable:
            mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = node.kelvin_min
            self._attr_max_color_temp_kelvin = node.kelvin_max
        elif ROLE_LIGHTNESS in node.models:
            mode = ColorMode.BRIGHTNESS
        else:
            mode = ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def is_on(self) -> bool | None:
        return self.record.state.on

    @property
    def brightness(self) -> int | None:
        if self._attr_color_mode == ColorMode.ONOFF or self.record.state.lightness is None:
            return None
        return lightness_to_brightness(self.record.state.lightness)

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._attr_color_mode != ColorMode.COLOR_TEMP:
            return None
        return self.record.state.kelvin

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        await self.coordinator.async_turn_on(
            self.record.subentry_id,
            lightness=None if brightness is None else brightness_to_lightness(brightness),
            kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
            transition_ms=transition_ms(kwargs),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off(
            self.record.subentry_id, transition_ms=transition_ms(kwargs)
        )
