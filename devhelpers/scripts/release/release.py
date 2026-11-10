"""Make a release's files, for a GitHub Release (.github/workflows/release.yml) or a test.

    python devhelpers/scripts/release/release.py --version 2.1.49 --base-url https://github.com/<owner>/<repo>/releases/download/v2.1.49

It reads what tools/dist built (dist/<version>/, with its SHA256SUMS) and the console's build
(ui/dist), and writes into --out (default dist/release/<version>/), flat, as a release holds them:

    ai-control-mesh-<version>.zip     the server bundle: VERSION, BUILD, server/ (its sources),
                                      ui/dist, docs/ and benchmarks/tests, what the server needs to run
    agent-<version>-<os>-<arch>.*     every archive tools/dist made (agent and agent-machine)
    ai_proxy_agent_py-<version>-*.whl the Python agent and agent-machine, for any system (required)
    install-local.sh, install-local.ps1   the web installers (templates beside this file), filled in
                                      with --base-url, the version and every download's SHA-256
                                      ({{BASE}}, {{VERSION}}, {{BUNDLE}}, {{FILES}}, {{WHEEL}})
    uninstall-local.sh, uninstall-local.ps1   what removes such an install again, as they are
    SHA256SUMS                        over all of the above

The installers trust only the hashes written into them, so a release's files can't be swapped
for others without changing the installer people download.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = Path(__file__).resolve().parent
SAFE_VERSION = re.compile(r"\d+\.\d+\.\d+(\+[0-9A-Za-z.]+)?")
SAFE_URL = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/+]*)?")
SAFE_NAME = re.compile(r"[A-Za-z0-9._+\-]+")
ARCHIVE = re.compile(r"(agent|agent-machine)-(?P<version>[^/]+)-(?P<platform>[a-z0-9]+-[a-z0-9]+)\.(zip|tar\.gz)")
# What the server needs at run time, as the bundle's paths. server/ is installed with pip -e, so
# the bundle's folder is the server's ROOT (config._find_root): VERSION and server/ are what it looks for.
BUNDLED = ("VERSION", "LICENSE", "server/pyproject.toml", "server/README.md", "server/agent_server", "ui/dist", "docs", "benchmarks/tests")
SKIPPED = {"__pycache__", ".pytest_cache"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sums(dist: Path) -> dict[str, str]:
    """SHA256SUMS of a tools/dist folder: path -> sha256."""
    listed = {}
    for line in (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, path = line.strip().partition(" ")
        if path:
            listed[path.lstrip(" *")] = digest
    return listed


def archives(dist: Path) -> list[tuple[str, str, str, Path]]:
    """(platform, component, sha256, file) for each agent and agent-machine archive SHA256SUMS lists."""
    found = []
    for path, digest in sums(dist).items():
        parts = path.split("/")
        match = ARCHIVE.fullmatch(parts[-1]) if len(parts) == 2 else None
        if not match or match.group("platform") != parts[0]:
            continue
        component = "agent-machine" if parts[1].startswith("agent-machine-") else "agent"
        found.append((parts[0], component, digest, dist / path))
    return sorted(found)


def bundle(version: str, target: Path, root: Path = ROOT) -> None:
    """The server bundle, a zip with the bundle's paths at its top (no folder around them)."""
    if not (root / "ui" / "dist" / "index.html").is_file():
        raise SystemExit("release: ui/dist isn't built (cd ui && npm ci && npm run build)")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BUILD", version + "\n")
        for name in BUNDLED:
            source = root / name
            if source.is_file():
                archive.write(source, name)
                continue
            if not source.is_dir():
                raise SystemExit(f"release: {name} is missing")
            for item in sorted(source.rglob("*")):
                relative = item.relative_to(root)
                if item.is_file() and not SKIPPED.intersection(relative.parts) and item.suffix != ".pyc":
                    archive.write(item, relative.as_posix())


def fill(template: str, base: str, version: str, bundle_line: str, files: list[str], windows: bool) -> str:
    text = (template.replace("\r\n", "\n")
            .replace("{{BASE}}", base).replace("{{VERSION}}", version)
            .replace("{{BUNDLE}}", bundle_line).replace("{{FILES}}", "\n".join(files)))
    # PowerShell reads CRLF fine; sh stops at `set -eu` with a CR after it.
    return text.replace("\n", "\r\n") if windows else text


def make(version: str, base: str, dist: Path, out: Path, root: Path = ROOT) -> list[Path]:
    if not SAFE_VERSION.fullmatch(version):
        raise SystemExit(f"release: {version!r} isn't X.Y.Z or X.Y.Z+<commit>")
    base = base.rstrip("/")
    if not SAFE_URL.fullmatch(base):
        raise SystemExit(f"release: the base URL {base!r} has characters an installer can't carry")
    if not (dist / "SHA256SUMS").is_file():
        raise SystemExit(f"release: {dist} has no SHA256SUMS (build it with devhelpers/scripts/build-dist.sh)")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    made: list[Path] = []

    name = f"ai-control-mesh-{version}.zip"
    bundle(version, out / name, root)
    bundle_line = f"{name} {sha256(out / name)}"
    made.append(out / name)

    files = {"sh": [], "ps1": []}
    for platform, component, digest, source in archives(dist):
        if sha256(source) != digest:
            raise SystemExit(f"release: {source} doesn't match its SHA256SUMS")
        if not SAFE_NAME.fullmatch(source.name):
            raise SystemExit(f"release: {source.name} can't be named in an installer")
        shutil.copy2(source, out / source.name)
        made.append(out / source.name)
        files["ps1" if platform.startswith("windows-") else "sh"].append(f"{platform} {component} {digest} {source.name}")
    if not files["sh"] and not files["ps1"]:
        raise SystemExit(f"release: {dist} has no agent or agent-machine archives")
    # The Python agent and agent-machine: the installers' --python, and any system without a Go build.
    wheels = sorted((dist / "python").glob("*.whl"))
    if len(wheels) != 1 or not SAFE_NAME.fullmatch(wheels[0].name):
        raise SystemExit(f"release: {dist / 'python'} needs the one Python wheel tools/dist builds (-python)")
    wheel = wheels[0]
    if sums(dist).get(f"python/{wheel.name}") != sha256(wheel):
        raise SystemExit(f"release: {wheel} doesn't match its SHA256SUMS")
    shutil.copy2(wheel, out / wheel.name)
    made.append(out / wheel.name)
    wheel_line = f"{wheel.name} {sha256(wheel)}"

    for kind in ("sh", "ps1"):
        template = (TEMPLATES / f"install-local.{kind}").read_text(encoding="utf-8")
        target = out / f"install-local.{kind}"
        text = fill(template, base, version, bundle_line, files[kind], kind == "ps1").replace("{{WHEEL}}", wheel_line)
        target.write_bytes(text.encode("utf-8"))
        made.append(target)

    # The uninstallers aren't templates: they find the install by its folders, not by a version.
    for kind in ("sh", "ps1"):
        text = (TEMPLATES / f"uninstall-local.{kind}").read_text(encoding="utf-8").replace("\r\n", "\n")
        target = out / f"uninstall-local.{kind}"
        target.write_bytes((text.replace("\n", "\r\n") if kind == "ps1" else text).encode("utf-8"))
        made.append(target)

    listing = "".join(f"{sha256(path)}  {path.name}\n" for path in made)
    (out / "SHA256SUMS").write_text(listing, encoding="utf-8", newline="\n")
    return [*made, out / "SHA256SUMS"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make a release's files from dist/<version>/ and ui/dist.")
    parser.add_argument("--version", required=True, help="the version tools/dist built (dist/<version>/)")
    parser.add_argument("--base-url", required=True, help="where the release's files will be downloaded from")
    parser.add_argument("--dist", type=Path, help="tools/dist's folder for the version (default dist/<version>)")
    parser.add_argument("--out", type=Path, help="where to write them (default dist/release/<version>), emptied first")
    args = parser.parse_args(argv)
    dist = args.dist or ROOT / "dist" / args.version
    out = args.out or ROOT / "dist" / "release" / args.version
    for path in make(args.version, args.base_url, dist, out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
