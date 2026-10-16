"""Regenerate the PWA icons in frontend/icons/ (requires Pillow: pip install pillow).

Usage: python scripts/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "frontend" / "icons"
BRAND = (79, 70, 229)  # #4f46e5


def icon(size: int, *, padding_ratio: float, radius_ratio: float, opaque_bg: bool) -> Image.Image:
    scale = 4  # draw large, downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), BRAND if opaque_bg else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = int(s * padding_ratio)
    draw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=int(s * radius_ratio), fill=BRAND)
    # Check mark, proportional to the inner square.
    inner = s - 2 * pad
    pts = [(pad + inner * 0.28, pad + inner * 0.52), (pad + inner * 0.43, pad + inner * 0.67), (pad + inner * 0.72, pad + inner * 0.36)]
    draw.line(pts, fill=(255, 255, 255), width=int(inner * 0.11), joint="curve")
    r = int(inner * 0.055)
    for x, y in (pts[0], pts[2]):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192, padding_ratio=0.0, radius_ratio=0.22, opaque_bg=False).save(OUT / "icon-192.png")
    icon(512, padding_ratio=0.0, radius_ratio=0.22, opaque_bg=False).save(OUT / "icon-512.png")
    # Maskable: content inside the 80% safe zone, full-bleed background.
    icon(512, padding_ratio=0.12, radius_ratio=0.0, opaque_bg=True).save(OUT / "icon-maskable-512.png")
    # iOS ignores transparency and applies its own mask; use an opaque square.
    icon(180, padding_ratio=0.0, radius_ratio=0.0, opaque_bg=True).convert("RGB").save(OUT / "apple-touch-icon.png")
    print(f"icons written to {OUT}")


if __name__ == "__main__":
    main()
