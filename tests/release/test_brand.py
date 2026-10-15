"""Brand images that HA's brands component serves from the integration folder."""

from __future__ import annotations

import struct

import pytest

from tests.release.test_metadata import INTEGRATION

BRAND = INTEGRATION / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(name: str) -> tuple[int, int]:
    data = (BRAND / name).read_bytes()
    assert data[:8] == PNG_SIGNATURE, f"{name} is not a PNG"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_images_have_the_sizes_ha_expects(name: str, size: int) -> None:
    assert png_size(name) == (size, size)


@pytest.mark.parametrize(
    ("name", "height"),
    [("logo.png", 128), ("logo@2x.png", 256), ("dark_logo.png", 128), ("dark_logo@2x.png", 256)],
)
def test_logos_are_landscape_at_the_brand_heights(name: str, height: int) -> None:
    width, actual = png_size(name)
    assert actual == height
    assert height < width <= 4 * height


def test_brand_images_are_small() -> None:
    for path in BRAND.glob("*.png"):
        assert path.stat().st_size < 200_000, f"{path.name} is too large"
