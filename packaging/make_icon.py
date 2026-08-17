"""Generate the app icon: flat stacked-slices motif on a dark squircle.

Outputs (in packaging/): icon_1024.png, icon.iconset/, icon.icns (macOS,
needs iconutil), icon.ico (Windows), and tiff_visualizer/assets/icon.png
for the in-app window icon.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ASSETS = HERE.parent / "tiff_visualizer" / "assets"

# Dark grid-of-tiles icon: the old 8x8 with each 2x2 block coalesced into one
# big square — a bold 3x3, columns in Italian-flag order (green, white, red),
# rows fading like descending z-slices.
BACKGROUND = (13, 13, 16, 255)
COLUMN_COLORS = [
    (55, 208, 92),  # green channel
    (232, 232, 238),  # white/brightfield
    (224, 68, 68),  # red channel
]
ROW_FADE = [1.0, 0.62, 0.38]  # top -> bottom


def _fade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int, int]:
    r, g, b = color
    bg = BACKGROUND
    return (
        int(bg[0] + (r - bg[0]) * factor),
        int(bg[1] + (g - bg[1]) * factor),
        int(bg[2] + (b - bg[2]) * factor),
        255,
    )


def draw_master(size: int = 1024, oversample: int = 4) -> Image.Image:
    s = size * oversample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Dark squircle background.
    margin = s * 0.045
    d.rounded_rectangle(
        [margin, margin, s - margin, s - margin], radius=int(s * 0.20), fill=BACKGROUND
    )

    # 3x3 grid of large rounded tiles.
    n = 3
    area = s * 0.70
    origin = (s - area) / 2
    pitch = area / n
    tile = pitch * 0.82
    radius = tile * 0.22
    for row in range(n):
        for col in range(n):
            color = _fade(COLUMN_COLORS[col], ROW_FADE[row])
            x = origin + col * pitch + (pitch - tile) / 2
            y = origin + row * pitch + (pitch - tile) / 2
            d.rounded_rectangle([x, y, x + tile, y + tile], radius=radius, fill=color)

    return img.resize((size, size), Image.LANCZOS)


def main():
    master = draw_master()
    master.save(HERE / "icon_1024.png")

    # In-app window icon (used on Windows taskbar / Linux).
    master.resize((512, 512), Image.LANCZOS).save(ASSETS / "icon.png")

    # Windows .ico
    master.save(
        HERE / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # macOS .icns via iconutil (mac only)
    if sys.platform == "darwin":
        iconset = HERE / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for pts in (16, 32, 128, 256, 512):
            master.resize((pts, pts), Image.LANCZOS).save(iconset / f"icon_{pts}x{pts}.png")
            master.resize((pts * 2, pts * 2), Image.LANCZOS).save(
                iconset / f"icon_{pts}x{pts}@2x.png"
            )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")],
            check=True,
        )
    print("icon assets written to", HERE)


if __name__ == "__main__":
    main()
