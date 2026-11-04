"""Puts each file's content hash in the brochure pages' links to it: /styles.css?v=<hash>,
/site.js?v=<hash> and /assets/screens/<name>.webp?v=<hash>.

Cloudflare and browsers keep these files for hours (max-age=14400), so a file changed under the
same link would go on being served old: a page with new markup and an old stylesheet. With the hash
in the link, every change gets a link of its own. Run it after changing site/styles.css,
site/site.js or the screenshots (scripts/screenshots/site-images.py runs it), before deploying;
scripts/site/check.mjs fails while a link's hash is stale.

    python scripts/site/fingerprint.py
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
PAGES = ("index.html", "teams.html", "security.html")
# A link to a file of the site's, with or without a ?v= already: the path from the site's root.
LINK = re.compile(r'(?P<path>/(?:styles\.css|site\.js|assets/screens/[A-Za-z0-9._-]+\.webp))(?:\?v=[0-9A-Za-z._-]+)?(?=["\s])')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def stamp(text: str) -> str:
    def stamped(match: re.Match[str]) -> str:
        path = SITE / match.group("path").lstrip("/")
        return f"{match.group('path')}?v={digest(path)}" if path.exists() else match.group(0)

    return LINK.sub(stamped, text)


def main() -> None:
    for name in PAGES:
        page = SITE / name
        # newline="" keeps the page's own line endings.
        with open(page, encoding="utf-8", newline="") as file:
            text = file.read()
        updated = stamp(text)
        if updated != text:
            with open(page, "w", encoding="utf-8", newline="") as file:
                file.write(updated)
            print(f"site/{name}: links fingerprinted")


if __name__ == "__main__":
    main()
