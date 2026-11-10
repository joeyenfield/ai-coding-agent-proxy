"""`agent-server downloads fetch`: a release's builds, put where the server hands them out.

A server run from the published image, or installed without Go, has nothing in dist/ for
/install.sh, the Downloads page and `agent update` to offer (downloads.py). tools/dist builds them
from a checkout; this fetches the ones a GitHub Release already holds instead: every agent and
agent-machine archive and the Python wheel, each checked against the release's SHA256SUMS, laid
out in dist/<version>/ as tools/dist leaves them (the archives per platform, their programs
unpacked into <platform>/artifact/ for `agent update`, and a SHA256SUMS over all of it).

    agent-server downloads fetch                     the release of this server's X.Y.Z
    agent-server downloads fetch --version 2.1.57    another one
    agent-server downloads fetch --from <url>        from somewhere else that holds a release's files

It's a command an administrator runs, so it reaches GitHub as any download would (the
environment's proxy settings apply). The running server never downloads anything by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import URLError
from urllib.request import urlopen

from . import version
from .config import ROOT
from .downloads import LINE, SUMS

RELEASES = "https://github.com/joeyenfield/ai-control-mesh/releases/download"
ARCHIVE = re.compile(r"(agent|agent-machine)-[0-9A-Za-z.+]+-(?P<platform>[a-z0-9]+-[a-z0-9]+)\.(zip|tar\.gz)")
WHEEL = re.compile(r"[A-Za-z0-9_]+-[0-9A-Za-z.+]+-py3-none-any\.whl")
PROGRAM = re.compile(r"[A-Za-z0-9._-]+")
SAFE_VERSION = re.compile(r"\d+\.\d+\.\d+")

Get = Callable[[str], bytes]


def _get(url: str) -> bytes:
    with urlopen(url, timeout=300) as response:
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _programs(name: str, data: bytes, platform: str) -> Iterator[tuple[str, bytes, int]]:
    """An archive's files as (name, content, mode): only plain files directly in its <platform>/ folder."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                parts = info.filename.split("/")
                if not info.is_dir() and len(parts) == 2 and parts[0] == platform and PROGRAM.fullmatch(parts[1]):
                    yield parts[1], archive.read(info), (info.external_attr >> 16) & 0o777 or 0o644
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = member.name.split("/")
            if member.isfile() and len(parts) == 2 and parts[0] == platform and PROGRAM.fullmatch(parts[1]):
                yield parts[1], archive.extractfile(member).read(), member.mode & 0o777


def fetch(root: Path, release: str, base: str, get: Get = _get, out: Any = sys.stdout) -> Path:
    """Put the release at `base` into root/<release>/. Nothing replaces an earlier folder until
    every file has been downloaded and has matched its SHA-256."""
    listed: dict[str, str] = {}
    for line in get(f"{base}/{SUMS}").decode("utf-8").splitlines():
        match = LINE.fullmatch(line.strip())
        if match:
            listed[match.group(2)] = match.group(1)
    wanted = sorted(name for name in listed if ARCHIVE.fullmatch(name) or WHEEL.fullmatch(name))
    if not any(ARCHIVE.fullmatch(name) for name in wanted):
        raise SystemExit(f"downloads fetch: {base}/{SUMS} lists no agent or agent-machine archives")
    staging = root / f".{release}.fetching"
    if staging.exists():
        shutil.rmtree(staging)
    sums: dict[str, str] = {}
    try:
        for name in wanted:
            print(f"Downloading {name}", file=out, flush=True)
            data = get(f"{base}/{name}")
            if _sha256(data) != listed[name]:
                raise SystemExit(f"downloads fetch: {name} doesn't match the release's SHA256SUMS; nothing was changed")
            match = ARCHIVE.fullmatch(name)
            folder = match.group("platform") if match else "python"
            (staging / folder).mkdir(parents=True, exist_ok=True)
            (staging / folder / name).write_bytes(data)
            sums[f"{folder}/{name}"] = listed[name]
            if not match:
                continue
            # What `agent update` and `agent-machine update` take: the programs themselves.
            (staging / folder / "artifact").mkdir(exist_ok=True)
            for program, content, mode in _programs(name, data, folder):
                target = staging / folder / "artifact" / program
                target.write_bytes(content)
                target.chmod(mode)
                sums[f"{folder}/artifact/{program}"] = _sha256(content)
        (staging / SUMS).write_text("".join(f"{digest}  {path}\n" for path, digest in sorted(sums.items())), encoding="utf-8", newline="\n")
        target = root / release
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-server downloads",
        description="Fetch a release's builds of agent and agent-machine into dist/, for the server to hand out "
                    "(/install.sh, the Downloads page, agent update). Each file is checked against the release's SHA256SUMS.",
    )
    parser.add_argument("action", choices=["fetch"])
    parser.add_argument("--version", help="The release to fetch (default: this server's X.Y.Z)")
    parser.add_argument("--from", dest="base", help=f"Where the release's files are (default: {RELEASES}/v<version>)")
    args = parser.parse_args(argv)
    release = args.version or version.current().split("+", 1)[0]
    if not SAFE_VERSION.fullmatch(release):
        print(f"downloads fetch: {release!r} isn't a release (X.Y.Z)", file=sys.stderr)
        return 2
    base = (args.base or f"{RELEASES}/v{release}").rstrip("/")
    root = Path(os.getenv("AI_PROXY_DIST_DIR", str(ROOT / "dist"))).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        target = fetch(root, release, base)
    except (URLError, OSError) as error:
        print(f"downloads fetch: {error} ({base}, into {root})", file=sys.stderr)
        return 1
    print(f"The server hands out {release} from {target}. A running server offers it at once.")
    if release != version.current():
        print(f"This server is {version.current()}: it offers the newest build of the same X.Y.Z when it has none of its own.")
    return 0
