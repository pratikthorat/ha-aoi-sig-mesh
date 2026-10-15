"""Draw the AoI Bluetooth SIG Mesh brand images into custom_components/aoi_sig_mesh/brand/.

Run once with any Python that has Pillow (HA's own venv has it):
    python tools/make_brand.py
The PNGs are committed; CI never runs this.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "aoi_sig_mesh" / "brand"
SCALE = 4  # draw large, then downsample, for smooth edges
NODE = (37, 99, 235, 255)  # mesh nodes
LINK = (37, 99, 235, 160)  # links between them
LIGHT = (245, 158, 11, 255)  # the lit centre node
HALO = (245, 158, 11, 70)
TEXT_LIGHT_THEME = (17, 24, 39, 255)
TEXT_DARK_THEME = (243, 244, 246, 255)
WORDMARK = "AoI SIG Mesh"
FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def mesh_icon(size: int) -> Image.Image:
    """Six nodes on a hexagon around a lit centre, each linked to its neighbours and the centre."""
    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    centre = big / 2
    ring = big * 0.36
    node_r = big * 0.085
    centre_r = big * 0.13
    points = [
        (
            centre + ring * math.cos(math.radians(90 + 60 * i)),
            centre - ring * math.sin(math.radians(90 + 60 * i)),
        )
        for i in range(6)
    ]
    width = max(1, round(big * 0.035))
    for i, point in enumerate(points):
        draw.line([point, points[(i + 1) % 6]], fill=LINK, width=width)
        draw.line([point, (centre, centre)], fill=LINK, width=width)
    for x, y in points:
        draw.ellipse([x - node_r, y - node_r, x + node_r, y + node_r], fill=NODE)
    halo = centre_r * 1.45
    draw.ellipse([centre - halo, centre - halo, centre + halo, centre + halo], fill=HALO)
    draw.ellipse(
        [centre - centre_r, centre - centre_r, centre + centre_r, centre + centre_r], fill=LIGHT
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def font(height: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, height)
    return ImageFont.load_default(size=height)


def logo(height: int, text_colour: tuple[int, int, int, int]) -> Image.Image:
    """The icon, then the wordmark, on a transparent background."""
    icon = mesh_icon(height)
    face = font(round(height * 0.38))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), WORDMARK, font=face)
    gap = round(height * 0.14)
    width = height + gap + (right - left) + round(height * 0.08)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.alpha_composite(icon, (0, 0))
    draw = ImageDraw.Draw(image)
    y = (height - (bottom - top)) / 2 - top
    draw.text((height + gap - left, y), WORDMARK, font=face, fill=text_colour)
    return image


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    mesh_icon(256).save(BRAND / "icon.png", optimize=True)
    mesh_icon(512).save(BRAND / "icon@2x.png", optimize=True)
    logo(128, TEXT_LIGHT_THEME).save(BRAND / "logo.png", optimize=True)
    logo(256, TEXT_LIGHT_THEME).save(BRAND / "logo@2x.png", optimize=True)
    logo(128, TEXT_DARK_THEME).save(BRAND / "dark_logo.png", optimize=True)
    logo(256, TEXT_DARK_THEME).save(BRAND / "dark_logo@2x.png", optimize=True)


if __name__ == "__main__":
    main()
