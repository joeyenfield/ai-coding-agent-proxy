"""The builds the server hands out: agent, agent-machine and the Android app, for every platform,
and the Python agent and agent-machine as one wheel.

tools/dist puts a version's builds in dist/<version>/ (see tools/dist/main.go), with a
SHA256SUMS over every file it made. The server offers the folder of its own version
(version.current()), or, when that wasn't built, the newest one with the same X.Y.Z,
and only the files SHA256SUMS lists: nothing else under dist/ is reachable. They're
public, like /health, because a machine needs them before it can sign in, and
`agent update` / `agent-machine update` fetch them the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import version


SUMS = "SHA256SUMS"
LINE = re.compile(r"([0-9a-f]{64}) [ *](\S.*)")
HINT = (
    "No builds for this version yet. Build them on the server's computer with scripts/build-dist.sh "
    "(.\\scripts\\build-dist.ps1 on Windows; PACKAGES=1 / -Packages for .deb, .rpm, .msi and the rest), "
    "or mount them into dist/ (AI_PROXY_DIST_DIR)."
)


@dataclass(frozen=True)
class Build:
    folder: Path
    version: str
    exact: bool
    sums: dict[str, str]


class Downloads:
    def __init__(self, root: Path):
        self.root = root

    def served(self) -> Build | None:
        """The build of the server's version, else the newest with the same X.Y.Z."""
        current = version.current()
        exact = self.root / current
        if _named(current) and (exact / SUMS).is_file():
            return Build(exact, current, True, _sums(exact))
        base = current.split("+", 1)[0]
        try:
            candidates = [
                folder for folder in self.root.iterdir()
                if folder.is_dir() and folder.name.split("+", 1)[0] == base and (folder / SUMS).is_file()
            ]
        except OSError:
            return None
        if not candidates:
            return None
        newest = max(candidates, key=lambda folder: (folder / SUMS).stat().st_mtime)
        return Build(newest, newest.name, False, _sums(newest))

    def index(self) -> dict[str, Any]:
        build = self.served()
        files = []
        if build:
            for path, digest in build.sums.items():
                target = build.folder / path
                if target.is_file():
                    files.append({**describe(path), "path": path, "size": target.stat().st_size, "sha256": digest, "url": f"/downloads/{build.version}/{path}"})
        return {
            "server_version": version.current(), "protocol": version.PROTOCOL,
            "version": build.version if build else None, "exact": bool(build and build.exact),
            "files": files, "hint": None if files else HINT,
        }

    def file(self, wanted: str, path: str) -> Path | None:
        """A file of the served build, only when its SHA256SUMS lists it."""
        build = self.served()
        if build is None or wanted != build.version or path not in build.sums:
            return None
        target = (build.folder / path).resolve()
        if not target.is_file() or not target.is_relative_to(build.folder.resolve()):
            return None
        return target


def describe(path: str) -> dict[str, Any]:
    """What a file in a build is, from where tools/dist puts it and how it names it."""
    parts = path.split("/")
    name = parts[-1]
    platform = parts[0]
    if platform == "android":
        return {"platform": "android", "os": "android", "arch": None, "component": "android", "kind": "android", "name": name}
    if platform == "python":
        # One wheel for every computer with Python: the Python agent and agent-machine (tools/dist/python.go).
        return {"platform": "python", "os": "python", "arch": None, "component": "python", "kind": "wheel" if name.endswith(".whl") else "file", "name": name}
    os_name, _, arch = platform.partition("-")
    if len(parts) == 3 and parts[1] == "artifact":
        program = name.removesuffix(".exe")
        kind = "installer" if program in {"install.sh", "install.ps1"} else "uninstaller" if program in {"uninstall.sh", "uninstall.ps1"} else "binary"
        component = "agent-machine" if program == "agent-machine" else ("agent" if kind == "binary" else None)
    else:
        component = "agent-machine" if name.startswith("agent-machine-") else "agent"
        kind = _kind(name, os_name)
    return {"platform": platform, "os": os_name, "arch": arch or None, "component": component, "kind": kind, "name": name}


def _kind(name: str, os_name: str) -> str:
    for suffix, kind in ((".pkg.tar.gz", "arch"), (".tar.gz", "archive"), (".zip", "archive"), (".deb", "deb"), (".rpm", "rpm"), (".msi", "msi"), (".pkg", "pkg")):
        if name.endswith(suffix):
            return kind
    if name.endswith(".apk"):
        return "apk" if os_name == "linux" else "android"
    return "file"


def _sums(folder: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    try:
        lines = (folder / SUMS).read_text(encoding="utf-8").splitlines()
    except OSError:
        return sums
    for line in lines:
        match = LINE.fullmatch(line.strip())
        if match and ".." not in match.group(2).split("/") and not match.group(2).startswith("/"):
            sums[match.group(2)] = match.group(1)
    return sums


def _named(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not any(char in value for char in "/\\:")
