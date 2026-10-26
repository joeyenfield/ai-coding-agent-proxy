"""The one-line installers: `curl -fsSL <server>/install.sh | sh` and `irm <server>/install.ps1 | iex`.

Each is a template (install/install.sh, install/install.ps1) the server fills in for the
request: its own address as the requester reached it, its build's version, and a table
of that build's archives (platform, component, SHA-256, path) from downloads.py. The
script works out the computer's system, downloads its archives from here, checks them,
runs the installer inside them, and signs the programs in to this server.

What goes into the script comes from the request's Host header and the server's files,
so it's checked to be plain URL and version characters before it's written in: a crafted
Host can't put shell or PowerShell into it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .downloads import Downloads

TEMPLATES = Path(__file__).with_name("install")
SAFE_URL = re.compile(r"https?://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?")
SAFE_TEXT = re.compile(r"[A-Za-z0-9._+\-/]+")


def script(kind: str, server: str, downloads: Downloads) -> str:
    """The installer for kind ("sh" or "ps1"), for clients reaching the server at server."""
    server = server.rstrip("/")
    if not SAFE_URL.fullmatch(server):
        raise ValueError("The server's address has characters an installer can't carry.")
    index = downloads.index()
    lines = []
    for item in index["files"]:
        if item["kind"] != "archive" or item["component"] not in {"agent", "agent-machine"}:
            continue
        if (item["os"] == "windows") != (kind == "ps1"):
            continue
        fields = [item["platform"], item["component"], item["sha256"], item["url"]]
        if all(SAFE_TEXT.fullmatch(str(field)) for field in fields):
            lines.append(" ".join(fields))
    version = str(index["version"] or index["server_version"])
    template = (TEMPLATES / f"install.{kind}").read_text(encoding="utf-8").replace("\r\n", "\n")
    text = (template.replace("{{SERVER}}", server)
            .replace("{{VERSION}}", version if SAFE_TEXT.fullmatch(version) else "unknown")
            .replace("{{FILES}}", "\n".join(lines)))
    # PowerShell reads a script fine with CRLF; sh needs LF.
    return text.replace("\n", "\r\n") if kind == "ps1" else text
