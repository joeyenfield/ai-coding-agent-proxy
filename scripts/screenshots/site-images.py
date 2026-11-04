"""Web-sized copies of the console captures for the brochure site (site/assets/screens/).

The captures in docs/screenshots are 2160 pixels wide (twice the 1080-pixel window they're taken
in), several hundred KB each as PNG. The site shows them at most about 1200 CSS pixels wide, so it
gets WebP copies at 800 and 1440 pixels (400 and 618 for the phone; srcset picks one), with the full PNG behind a link.
Run it after capture.mjs changes one of the captures below:

    python scripts/screenshots/site-images.py
"""
from __future__ import annotations

import subprocess
import sys
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
    # A part of the capture, as shares of its width and height (left, top, right, bottom): the hero
    # leaves the console's sidebar out, so its cards read larger.
    box: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    # The phone capture is narrow: it gets smaller copies, never wider than itself.
    widths: tuple[int, ...] = WIDTHS


# The console's sidebar is the first 17.5% of a desktop capture: the site leaves it out, so what
# each capture is about reads larger.
NO_SIDEBAR = (0.175, 0.0, 1.0, 1.0)

SHOTS = (
    Shot("terminals-dark", box=(0.18, 0.0, 1.0, 0.84)),
    Shot("live-dark", box=(0.175, 0.0, 1.0, 0.95)),
    Shot("terminal-phone", widths=(400, 618)),
    # Three real agents' terminals side by side: OpenCode, GitHub Copilot CLI and Qwen Code started
    # from the production console on a fleet machine, on its Ollama (gpt-oss:20b), captured from the
    # console's terminal view (only the terminal, after Fit width). Not made by capture.mjs.
    Shot("real-opencode", widths=(666, 1333)),
    Shot("real-copilot", widths=(666, 1333)),
    Shot("real-qwen", widths=(666, 1333)),
    Shot("session-chat", box=NO_SIDEBAR),
    Shot("agents", box=NO_SIDEBAR),
    Shot("machines", box=NO_SIDEBAR),
    Shot("machine-mcp", box=NO_SIDEBAR),
    Shot("terminal"),
    Shot("overview-dark", keep=0.35),
    # quickstart.html's steps: the real quickstart, from quickstart-steps.mjs, whole (the sidebar
    # shows where each page is).
    # The two sign-in pages are a small card on an empty page: just the card.
    Shot("quickstart-01-sign-in", box=(0.25, 0.15, 0.75, 0.85), widths=(540, 1080)),
    Shot("quickstart-02-issuer", box=(0.25, 0.15, 0.75, 0.85), widths=(540, 1080)),
    *(Shot(f"quickstart-{name}") for name in (
        "03-machines", "04-model-servers", "05-add-provider", "06-search",
        "07-results", "08-pulled", "09-start-agent", "10-agent-ready", "11-answer", "12-requests")),
)


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        with Image.open(SOURCE / f"{shot.name}.png") as image:
            image = image.convert("RGB")
            if shot.keep < 1:
                image = image.crop((0, 0, image.width, round(image.height * shot.keep)))
            left, top, right, bottom = shot.box
            if shot.box != (0.0, 0.0, 1.0, 1.0):
                image = image.crop((round(image.width * left), round(image.height * top), round(image.width * right), round(image.height * bottom)))
            for width in shot.widths:
                height = round(image.height * width / image.width)
                out = TARGET / f"{shot.name}-{width}.webp"
                image.resize((width, height), Image.LANCZOS).save(out, "WEBP", quality=82, method=6)
                print(f"{out.relative_to(ROOT)}  {width}x{height}  {out.stat().st_size // 1024} KB")
    # The pages link to each copy by its content hash (scripts/site/fingerprint.py).
    subprocess.run([sys.executable, str(ROOT / "scripts" / "site" / "fingerprint.py")], check=True)



if __name__ == "__main__":
    main()
