"""Local release boundaries: identity, publish gates, selected builds and failure handling."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "local.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("local_releases", SCRIPT)
local = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = local
spec.loader.exec_module(local)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("2.1.61")
    values = {("rev-parse", "HEAD"): "a" * 40,
              ("status", "--porcelain", "--untracked-files=no"): "",
              ("tag", "--points-at", "HEAD"): "v2.1.61"}
    monkeypatch.setattr(local, "git", lambda root, *args: values[args])
    return tmp_path, values


def test_snapshots_have_a_python_version_and_distinct_docker_tags(checkout):
    root, _ = checkout
    first = local.identify(root, False, datetime(2026, 11, 11, 22, 0, 0, 1, tzinfo=timezone.utc))
    second = local.identify(root, False, datetime(2026, 11, 11, 22, 0, 0, 2, tzinfo=timezone.utc))
    assert first.tag != second.tag
    assert local.release.SAFE_VERSION.fullmatch(first.version)
    assert "+" not in first.tag and len(first.tag) <= 128
    assert first.version.startswith("2.1.61+snapshot.")


def test_stable_builds_require_the_matching_tag_and_clean_source(checkout):
    root, values = checkout
    assert local.identify(root, True).version == "2.1.61"
    values[("tag", "--points-at", "HEAD")] = "v2.1.60"
    with pytest.raises(ValueError, match="existing vVERSION tag"):
        local.identify(root, True)
    values[("tag", "--points-at", "HEAD")] = "v2.1.61"
    values[("status", "--porcelain", "--untracked-files=no")] = " M site/index.html"
    with pytest.raises(ValueError, match="clean checkout"):
        local.identify(root, True)


def test_default_builds_do_not_publish_and_keep_the_types_separate(checkout, monkeypatch):
    root, _ = checkout
    commands = []
    monkeypatch.setattr(local.Runner, "run", lambda self, *args, **kw: commands.append(args))
    assert local.main(["website"], root) == 0
    assert len(commands) == 1 and "site/Dockerfile" in commands[0]
    commands.clear()
    assert local.main(["services"], root) == 0
    assert len(commands) == 1 and "server/Dockerfile" in commands[0]
    assert "linux/amd64" in commands[0]
    for kind in ("website", "services"):
        record = json.loads(next((root / "dist/releases" / kind).glob("*/release.json")).read_text())
        assert record["published"] is False


def test_a_failed_build_does_not_start_publishing(checkout, monkeypatch):
    root, _ = checkout
    commands = []

    def execute(self, *args, **kwargs):
        commands.append(args)
        if "sandbox/Dockerfile" in args:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(local.Runner, "run", execute)
    assert local.main(["services", "--image", "server", "--image", "sandbox", "--publish"], root) == 1
    assert len(commands) == 2
    assert not list(root.glob("dist/releases/services/*/release.json"))
    assert all(args[:2] == ("podman", "build") for args in commands)


def test_automation_receipt_is_written_only_after_success(checkout, monkeypatch):
    root, _ = checkout
    receipt = root / "automation" / "services.json"
    monkeypatch.setattr(local.Runner, "run", lambda self, *a, **kw: None)
    assert local.main(["services", "--receipt", str(receipt)], root) == 0
    original = receipt.read_bytes()
    record = json.loads(original)
    assert record["images"][0]["reference"].endswith(":" + record["tag"])

    def fail(self, *args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(local.Runner, "run", fail)
    assert local.main(["services", "--receipt", str(receipt)], root) == 1
    assert receipt.read_bytes() == original


def test_snapshot_publishing_cannot_promote_latest(checkout, monkeypatch):
    root, values = checkout
    commands = []
    monkeypatch.setattr(local.Runner, "run", lambda self, *args, **kw: commands.append(args))
    assert local.main(["website", "--promote", "--publish"], root) == 1
    assert not commands
    values[("status", "--porcelain", "--untracked-files=no")] = " M site/index.html"
    assert local.main(["website", "--publish"], root) == 1
    assert not commands


def test_multiple_platforms_are_published_together_only_for_selected_images(checkout, monkeypatch):
    root, _ = checkout
    commands = []
    monkeypatch.setattr(local.Runner, "run", lambda self, *args, **kw: commands.append(args))
    assert local.main(["services", "--image", "gpu", "--platform", "linux/amd64",
                       "--platform", "linux/arm64", "--publish"], root) == 0
    build, push = commands
    assert "--manifest" in build and "OLLAMA_GPU=cuda_v13" in build
    assert push[:3] == ("podman", "manifest", "push")
    assert "--all" in push and "v2s2" in push
    assert all(":latest" not in word for word in push)


def test_plan_does_not_build_publish_or_write_output(checkout, monkeypatch):
    root, _ = checkout
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw: pytest.fail("plan executed a command"))
    assert local.main(["website", "--publish", "--plan"], root) == 0
    assert not (root / "dist").exists()


def test_installer_snapshot_is_a_prerelease_and_uses_explicit_source_commit(checkout, monkeypatch):
    root, _ = checkout
    commands = []
    # Plan supplies a placeholder for generated files while allowing command inspection.
    monkeypatch.setattr(local.Runner, "run", lambda self, *args, **kw: commands.append(args))
    assert local.main(["installers", "--target", "linux/amd64", "--publish", "--plan"], root) == 0
    assert commands[2][0] == "go" and "linux/amd64" in commands[2]
    publish = commands[-1]
    assert publish[:3] == ("gh", "release", "create")
    assert "--prerelease" in publish and "--latest=false" in publish
    assert publish[publish.index("--target") + 1] == "a" * 40


def test_invalid_options_fail_before_building(checkout, monkeypatch):
    root, _ = checkout
    monkeypatch.setattr(local.Runner, "run", lambda *a, **kw: pytest.fail("invalid options built something"))
    assert local.main(["website", "--image", "server"], root) == 1
    assert local.main(["installers", "--archive"], root) == 1
    assert local.main(["website", "--platform", "linux/amd64", "--platform", "linux/arm64", "--archive"], root) == 1
