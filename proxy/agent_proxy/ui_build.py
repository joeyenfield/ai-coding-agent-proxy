"""Rebuild the web UI when its sources are newer than the built copy.

agent-proxy calls this before it starts serving, so a checkout with UI changes
never serves a stale ui/dist. A failed or impossible build only logs a warning;
the proxy then serves whatever build already exists.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


# Paths under the UI project whose changes affect the build.
SOURCES = ("src", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")


def ensure_built(dist: Path) -> bool:
    """Build the UI if needed. Returns True when an up-to-date build exists afterwards."""
    project = dist.parent
    if not (project / "package.json").is_file():
        # A custom AI_PROXY_UI_DIR with no sources next to it: nothing to build.
        return (dist / "index.html").is_file()
    if not stale(dist):
        return True
    npm = shutil.which("npm")
    if not npm:
        _warn("UI sources changed but npm isn't on PATH, so the UI wasn't rebuilt.")
        return (dist / "index.html").is_file()
    steps = []
    # npm rewrites node_modules/.package-lock.json on every install.
    if _newest(project / "package-lock.json", project / "package.json") > _mtime(project / "node_modules" / ".package-lock.json"):
        steps.append([npm, "install", "--no-audit", "--no-fund"])
    steps.append([npm, "run", "build"])
    print("Rebuilding the web UI (pass --no-build to skip)...", file=sys.stderr, flush=True)
    for command in steps:
        try:
            result = subprocess.run(command, cwd=project, capture_output=True, text=True, check=False)
        except OSError as exc:
            _warn(f"Couldn't run {' '.join(command[1:])}: {exc}")
            return (dist / "index.html").is_file()
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip().splitlines()[-20:]
            _warn(f"`npm {' '.join(command[1:])}` failed; serving the previous build.\n" + "\n".join(output))
            return (dist / "index.html").is_file()
    print("Web UI rebuilt.", file=sys.stderr, flush=True)
    return True


def stale(dist: Path) -> bool:
    built = _mtime(dist / "index.html")
    if built == 0:
        return True
    project = dist.parent
    return _newest(*(project / name for name in SOURCES)) > built


def _newest(*paths: Path) -> float:
    newest = 0.0
    for path in paths:
        if path.is_dir():
            newest = max([newest, *(_mtime(item) for item in path.rglob("*") if item.is_file())])
        else:
            newest = max(newest, _mtime(path))
    return newest


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _warn(message: str) -> None:
    print(f"agent-proxy: {message}", file=sys.stderr, flush=True)
