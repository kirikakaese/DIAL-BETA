"""Generate the PWA icons in static/icons/ with Pillow (no font needed).

Usage: .venv/bin/python scripts/make_icons.py

The mark is a stylised handset (an arch with two round ends) on the DIAL primary blue. The geometry mirrors
static/icons/favicon.svg, so all icons look alike. Regular icons are rounded squares with transparent
corners; the maskable icon and the Apple touch icon are full-bleed (the OS applies its own mask).
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
BG = "#3b82f6"
FG = "#ffffff"
SS = 4  # supersampling factor for smooth edges


def _mark(draw: ImageDraw.ImageDraw, size: int, scale: float) -> None:
    """Handset mark in a ``size``x``size`` box, geometry defined on a 512 grid."""
    k = size / 512 * scale
    cx, cy = size / 2, size / 2 + 70 * k
    r, width, tip = 150 * k, 56 * k, 62 * k
    # the "handle": upper 140° of a circle, from 200° to 340° (PIL angles run clockwise, 0° = 3 o'clock)
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=340, fill=FG, width=int(round(width)))
    # ear and mouth pieces at the arc ends (cos/sin of 200° and 340°: ±0.9397, -0.3420)
    for sx in (-1, 1):
        ex, ey = cx + sx * 0.9397 * r, cy - 0.3420 * r
        draw.ellipse([ex - tip, ey - tip, ex + tip, ey + tip], fill=FG)


def icon(size: int, *, rounded: bool, scale: float = 1.0) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=BG)
    else:
        draw.rectangle([0, 0, big - 1, big - 1], fill=BG)
    _mark(draw, big, scale)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "dial-192.png": icon(192, rounded=True),
        "dial-512.png": icon(512, rounded=True),
        # maskable: full bleed, mark shrunk into the 80% safe zone
        "dial-maskable-512.png": icon(512, rounded=False, scale=0.78),
        "apple-touch-icon.png": icon(180, rounded=False, scale=0.9).convert("RGB"),
    }
    for name, img in files.items():
        img.save(OUT / name, format="PNG", optimize=True)
        print(f"wrote {name} ({(OUT / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
