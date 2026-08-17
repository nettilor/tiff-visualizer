"""Renders the disk-image window background: the drag-to-Applications arrow
and the first-launch Gatekeeper steps.

Finder draws this behind the real icons, whose positions are set by
make_dmg.sh and must agree with the geometry here.

Usage: python packaging/make_dmg_background.py <output-directory>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 640, 420

# The DMG window is a light surface whatever the app's own theme is: Finder
# draws the icon labels in the *system* appearance's colors, so a dark
# background would hide them for everyone in light mode.
PAPER = (255, 255, 255)
ARROW = (168, 168, 168)
HEADLINE = (64, 64, 64)
PANEL_FILL = (246, 246, 246)
PANEL_EDGE = (214, 214, 214)
PANEL_TITLE = (51, 51, 51)
PANEL_TEXT = (89, 89, 89)
FOOTNOTE = (150, 150, 150)

_SF = "/System/Library/Fonts/SFNS.ttf"
_FALLBACK = "/System/Library/Fonts/HelveticaNeue.ttc"
_WEIGHTS = {"Regular": 400, "Medium": 500, "Semibold": 590}
_FALLBACK_FACES = {"Regular": 0, "Medium": 10, "Semibold": 1}


def font(size: float, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """San Francisco at a given weight, falling back through Helvetica Neue to
    Pillow's bitmap default — a missing font must not fail the release build.

    SF is a variable font whose Optical Size axis defaults to 28: left alone it
    letterspaces 12 pt body text like a headline, and words visibly run
    together. Pinning the axis to the actual size is the fix.
    """
    try:
        f = ImageFont.truetype(_SF, size)
        # Axis order as reported by get_variation_axes(): width, optical size,
        # grade, weight.
        f.set_variation_by_axes([100, size, 400, _WEIGHTS.get(weight, 400)])
        return f
    except (OSError, AttributeError, ValueError):
        pass
    try:
        return ImageFont.truetype(_FALLBACK, size, index=_FALLBACK_FACES.get(weight, 0))
    except OSError:
        return ImageFont.load_default()


def draw_background() -> Image.Image:
    image = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(image)

    # The arrow between where the app icon sits and where Applications sits.
    y = 160
    d.line([(240, y), (384, y)], fill=ARROW, width=5)
    d.polygon([(380, y - 12), (404, y), (380, y + 12)], fill=ARROW)

    d.text((W / 2, 248), "Drag TIFF Visualizer into Applications",
           font=font(17, "Medium"), fill=HEADLINE, anchor="ma")

    # The first-launch panel: the app is not signed with an Apple developer
    # certificate, so every user meets Gatekeeper once and should be told
    # exactly what to click before it happens.
    d.rounded_rectangle([28, 286, W - 28, 286 + 106], radius=12,
                        fill=PANEL_FILL, outline=PANEL_EDGE, width=1)
    d.text((48, 298), "The first time you open it",
           font=font(14, "Semibold"), fill=PANEL_TITLE)
    steps = [
        "1   macOS will say it could not verify TIFF Visualizer — click Done, not Move to Trash.",
        "2   Open System Settings → Privacy & Security, scroll down, and click Open Anyway.",
        "3   That is the whole ritual, and it is only ever asked for once.",
    ]
    for i, step in enumerate(steps):
        d.text((48, 324 + i * 20), step, font=font(12), fill=PANEL_TEXT)

    d.text((W / 2, 400), "Apple Silicon Mac, macOS 12 or later",
           font=font(11), fill=FOOTNOTE, anchor="ma")
    return image


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    target = out / "background.tiff"
    # One pixel per point, deliberately: Finder draws window backgrounds at
    # pixel size and ignores the image's resolution tags, so a 2x image comes
    # out double and shows only its left half.
    draw_background().save(target, format="TIFF")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
