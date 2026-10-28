"""`update` for both programs: the wheel the agent-server offers (/downloads/index.json, the
python/ file of the build it serves), checked against the SHA-256 the index lists, installed with
pip into the Python running this. The Go programs replace their binaries instead (client/internal/update).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote

import httpx

from . import common


class UpdateError(Exception):
    pass


def fetch(server: str, program: str) -> Dict[str, Any]:
    """The server's downloads index; public, like the files."""
    try:
        with common.client(30, program) as http:
            response = http.get(server.rstrip("/") + "/downloads/index.json")
    except httpx.HTTPError as error:
        raise UpdateError(f"couldn't reach the agent-server at {server}: {error}")
    if response.status_code >= 400:
        raise UpdateError(f"the agent-server at {server} answered {response.status_code}: {common.detail(response.content)}")
    try:
        index = response.json()
    except ValueError:
        raise UpdateError(f"the agent-server at {server} sent no downloads index")
    return index if isinstance(index, dict) else {}


def wheel(index: Dict[str, Any], server: str) -> Dict[str, Any]:
    for item in index.get("files") or []:
        if isinstance(item, dict) and item.get("component") == "python" and item.get("kind") == "wheel":
            return item
    hint = index.get("hint") or "build it with scripts/build-dist on the server's computer"
    raise UpdateError(f"the agent-server at {server} offers no Python build ({hint})")


def install(server: str, index: Dict[str, Any], program: str) -> str:
    """Downloads the server's wheel, checks it, and installs it with pip. Returns the version installed."""
    item = wheel(index, server)
    name = Path(str(item.get("name") or "")).name
    if not name.endswith(".whl"):
        raise UpdateError("the server's Python build isn't a wheel")
    with tempfile.TemporaryDirectory(prefix="agent-update-") as folder:
        # pip reads the version from the file's name, so it keeps the server's.
        path = Path(folder) / name
        url = server.rstrip("/") + quote(str(item.get("url") or ""), safe="/+:")
        try:
            with common.client(None, program) as http, http.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise UpdateError(f"the agent-server answered {response.status_code} for {name}")
                digest = hashlib.sha256()
                with open(path, "wb") as file:
                    for chunk in response.iter_bytes():
                        digest.update(chunk)
                        file.write(chunk)
        except httpx.HTTPError as error:
            raise UpdateError(f"couldn't download {name}: {error}")
        if digest.hexdigest() != str(item.get("sha256") or ""):
            raise UpdateError(f"{name} doesn't match the SHA-256 the server lists; nothing was installed")
        done = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--no-input", str(path)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()[-5:]
            raise UpdateError(f"pip couldn't install {name}:\n" + "\n".join(detail))
    return str(index.get("version") or "")


def notes(index: Dict[str, Any]) -> str:
    if index.get("exact") is False and index.get("server_version"):
        return f"Note: the server runs {index['server_version']}; this is its closest build ({index.get('version')}).\n"
    return ""


def checkout() -> str:
    """The checkout this copy runs from, if it does (then a folder, not the installed package, is what runs)."""
    for parent in Path(common.__file__).resolve().parents:
        if (parent / "VERSION").is_file() and (parent / "server").is_dir():
            return str(parent)
    return ""


def current() -> str:
    """The version an update replaces: the installed package's when this copy runs from a checkout (the
    checkout's own version says what its source is, not what pip installed), else this copy's."""
    if checkout():
        try:
            from importlib.metadata import version

            return version("ai-proxy-agent-py")
        except Exception:
            return ""
    return common.VERSION
