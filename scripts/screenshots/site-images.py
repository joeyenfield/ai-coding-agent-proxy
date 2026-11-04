"""Web-sized copies of the console captures for the brochure site (site/assets/screens/).

The captures in docs/screenshots are 2160 pixels wide (twice the 1080-pixel window they're taken
in), several hundred KB each as PNG. The site shows them at most about 1200 CSS pixels wide, so it
gets WebP copies at 800 and 1440 pixels (srcset picks one), with the full PNG behind a link.
Run it after capture.mjs changes one of the captures below:

    python scripts/screenshots/site-images.py
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "screenshots"
TARGET = ROOT / "site" / "assets" / "screens"
WIDTHS = (800, 1440)


@dataclass(frozen=True)
class Shot:
    name: str
    # The share of the capture's height to keep from the top (1 = all of it): the dark overview is a
    # long page, and the site shows its first screen.
    keep: float = 1.0


SHOTS = (
    Shot("session-chat"),
    Shot("agents"),
    Shot("machines"),
    Shot("machine-mcp"),
    Shot("terminal"),
    Shot("overview-dark", keep=0.35),
)


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        with Image.open(SOURCE / f"{shot.name}.png") as image:
            image = image.convert("RGB")
            if shot.keep < 1:
                image = image.crop((0, 0, image.width, round(image.height * shot.keep)))
            for width in WIDTHS:
                height = round(image.height * width / image.width)
                out = TARGET / f"{shot.name}-{width}.webp"
                image.resize((width, height), Image.LANCZOS).save(out, "WEBP", quality=82, method=6)
                print(f"{out.relative_to(ROOT)}  {width}x{height}  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
