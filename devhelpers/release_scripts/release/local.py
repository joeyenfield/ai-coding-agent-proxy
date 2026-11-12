"""Build website, service images or installer releases locally, without GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import release

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Image:
    name: str
    dockerfile: str
    arguments: tuple[str, ...] = ()


IMAGES = {
    "website": Image("ai-control-mesh-site", "site/Dockerfile"),
    "server": Image("agent-server", "server/Dockerfile"),
    "sandbox": Image("agent-sandbox", "sandbox/Dockerfile"),
    "allinone": Image("ai-control-mesh", "devhelpers/allinone/Dockerfile"),
    "gpu": Image("ai-control-mesh-gpu", "devhelpers/allinone/Dockerfile", ("OLLAMA_GPU=cuda_v13",)),
}


def git(root: Path, *args: str) -> str:
    # A per-command exception avoids changing the developer's global Git configuration.
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={root.as_posix()}", *args], cwd=root, text=True,
    ).strip()


@dataclass(frozen=True)
class Build:
    version: str
    tag: str
    commit: str
    dirty: bool


def identify(root: Path, stable: bool, now: datetime | None = None) -> Build:
    base = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", base):
        raise ValueError("VERSION must contain X.Y.Z")
    commit = git(root, "rev-parse", "HEAD")
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=no"))
    if stable:
        tags = git(root, "tag", "--points-at", "HEAD").splitlines()
        if dirty or f"v{base}" not in tags:
            raise ValueError("--stable needs a clean checkout at the existing vVERSION tag")
        return Build(base, base, commit, False)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S%f")
    suffix = f"snapshot.{stamp}.g{commit[:7]}" + (".dirty" if dirty else "")
    return Build(f"{base}+{suffix}", suffix.replace(".", "-"), commit, dirty)


class Runner:
    def __init__(self, root: Path, plan: bool = False):
        self.root = root
        self.plan = plan

    def run(self, *args: str, cwd: Path | None = None) -> None:
        print("+ " + shlex.join(str(arg) for arg in args), flush=True)
        if not self.plan:
            command = list(args)
            # Windows installs npm as npm.cmd; resolve it without shell interpolation.
            command[0] = shutil.which(command[0]) or command[0]
            env = {**os.environ, "GOTOOLCHAIN": "local"}
            subprocess.run(command, cwd=cwd or self.root, env=env, check=True)


def image_builds(args: argparse.Namespace, build: Build, runner: Runner, out: Path) -> list[dict]:
    selected = ["website"] if args.kind == "website" else (args.image or ["server"])
    platforms = list(dict.fromkeys(args.platform or ["linux/amd64"]))
    if args.archive and len(platforms) != 1:
        raise ValueError("--archive currently needs one platform; publish a manifest for multiple platforms")
    images = [IMAGES[key] for key in dict.fromkeys(selected)]
    records = []
    for image in images:
        reference = f"{args.registry}/{image.name}:{build.tag}"
        command = ["podman", "build", "--http-proxy=false", "--format", "docker",
                   "--platform", ",".join(platforms)]
        command += ["--manifest" if len(platforms) > 1 else "--tag", reference]
        command += ["--file", image.dockerfile]
        if image.dockerfile != "site/Dockerfile":
            command += ["--build-arg", f"AI_PROXY_VERSION={build.version}"]
        command += ["--label", f"org.opencontainers.image.version={build.version}",
                    "--label", f"org.opencontainers.image.revision={build.commit}"]
        for argument in image.arguments:
            command += ["--build-arg", argument]
        runner.run(*command, ".")
        if args.archive:
            runner.run("podman", "save", "--format", "docker-archive", "--output",
                       str(out / f"{image.name}.tar"), reference)
        records.append({"name": image.name, "reference": reference, "platforms": platforms})
    # Finish every selected build before publishing anything.
    if args.publish:
        for record in records:
            reference = record["reference"]
            if len(platforms) > 1:
                runner.run("podman", "manifest", "push", "--all", "--format", "v2s2",
                           reference, f"docker://{reference}")
            else:
                runner.run("podman", "push", "--format", "v2s2", reference, f"docker://{reference}")
        # Snapshots never replace latest. Stable promotion is a separate, explicit choice.
        if args.promote:
            for record in records:
                reference = record["reference"]
                latest = reference.rsplit(":", 1)[0] + ":latest"
                if len(platforms) > 1:
                    runner.run("podman", "manifest", "push", "--all", "--format", "v2s2",
                               reference, f"docker://{latest}")
                else:
                    runner.run("podman", "push", "--format", "v2s2", reference, f"docker://{latest}")
    return records


def installers(args: argparse.Namespace, build: Build, runner: Runner, out: Path) -> None:
    tag = f"v{build.version}" if args.stable else build.tag
    base = args.base_url or f"https://github.com/{args.repo}/releases/download/{tag}"
    if not release.SAFE_URL.fullmatch(base.rstrip("/")):
        raise ValueError("--base-url must be a plain HTTP(S) URL supported by the installers")
    if args.publish and args.base_url:
        raise ValueError("--base-url is for self-hosted files; omit it when publishing to GitHub")
    runner.run("npm", "ci", "--no-audit", "--no-fund", cwd=runner.root / "ui")
    runner.run("npm", "run", "build", cwd=runner.root / "ui")
    runner.run("go", "run", "./dist", "-version", build.version, "-android=false",
               "-out", "dist/local-builds", *(args.target or []), cwd=runner.root / "tools")
    runner.run(sys.executable, "devhelpers/release_scripts/release/release.py", "--version", build.version,
               "--base-url", base, "--dist", str(runner.root / "dist/local-builds" / build.version),
               "--out", str(out / "files"))
    if args.publish:
        files = sorted((out / "files").iterdir()) if not runner.plan else [out / "files" / "<release-files>"]
        command = ["gh", "release", "create", tag, "--repo", args.repo, "--title", build.version,
                   "--notes", f"Locally built {'release' if args.stable else 'snapshot'} of {build.commit}."]
        command += ["--verify-tag"] if args.stable else ["--target", build.commit, "--prerelease"]
        command += ["--latest" if args.promote else "--latest=false"]
        runner.run(*command, *(str(path) for path in files))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("kind", choices=["website", "services", "installers"])
    result.add_argument("--image", action="append", choices=["server", "sandbox", "allinone", "gpu"],
                        help="services only; repeat to select images (default server, including auth and console)")
    result.add_argument("--platform", action="append", choices=["linux/amd64", "linux/arm64"],
                        help="image builds only; repeat for a manifest (default linux/amd64)")
    result.add_argument("--target", action="append", help="installer Go target; repeat (default all 15)")
    result.add_argument("--registry", default="ghcr.io/joeyenfield", help="image registry/owner; login with podman login")
    result.add_argument("--repo", default="joeyenfield/ai-control-mesh", help="GitHub repository for installer downloads")
    result.add_argument("--base-url", help="self-hosted installer file download URL")
    result.add_argument("--archive", action="store_true", help="also save each image as a Docker archive")
    result.add_argument("--publish", action="store_true", help="push images or upload a GitHub Release directly")
    result.add_argument("--stable", action="store_true", help="build VERSION from its existing clean release tag")
    result.add_argument("--promote", action="store_true", help="with --stable --publish, also update latest")
    result.add_argument("--plan", action="store_true", help="print commands without building, writing files or publishing")
    result.add_argument("--receipt", type=Path, help="also write the success receipt here for deployment automation")
    return result


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.promote and not (args.stable and args.publish):
            raise ValueError("--promote requires --stable --publish")
        if args.image and args.kind != "services":
            raise ValueError("--image applies only to services")
        if args.kind == "installers" and (args.platform or args.archive):
            raise ValueError("installers use --target rather than --platform or --archive")
        if args.kind != "installers" and (args.target or args.base_url):
            raise ValueError("--target and --base-url apply only to installers")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.:-]*(/[a-z0-9][a-z0-9._-]*)+", args.registry):
            raise ValueError("--registry must be a lower-case registry/owner path")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
            raise ValueError("--repo must be owner/repository")
        build = identify(root, args.stable)
        if args.publish and build.dirty:
            raise ValueError("commit tracked changes before publishing; local dirty builds are allowed")
        out = root / "dist" / "releases" / args.kind / build.tag
        runner = Runner(root, args.plan)
        print(f"{args.kind}: {build.version}\nTag: {build.tag}\nOutput: {out}", flush=True)
        if not args.plan:
            out.mkdir(parents=True, exist_ok=False)
        record = {"kind": args.kind, "version": build.version, "tag": build.tag,
                  "commit": build.commit, "dirty": build.dirty, "published": False}
        if args.kind == "installers":
            installers(args, build, runner, out)
        else:
            record["images"] = image_builds(args, build, runner, out)
        record["published"] = args.publish
        if not args.plan:
            receipt = json.dumps(record, indent=2) + "\n"
            (out / "release.json").write_text(receipt, encoding="utf-8")
            paths = sorted(path for path in out.rglob("*") if path.is_file())
            (out / "SHA256SUMS").write_text("".join(
                f"{release.sha256(path)}  {path.relative_to(out).as_posix()}\n" for path in paths
            ), encoding="utf-8")
            if args.receipt:
                args.receipt.parent.mkdir(parents=True, exist_ok=True)
                args.receipt.write_text(receipt, encoding="utf-8")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"local release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
