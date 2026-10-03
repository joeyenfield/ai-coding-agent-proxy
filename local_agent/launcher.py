from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from local_agent.config import Settings


def ensure_proxy(
    proxy_url: str,
    listen_host: str = "127.0.0.1",
    timeout: float = 15,
) -> subprocess.Popen[Any] | None:
    if _healthy(proxy_url):
        return None
    parsed = urlparse(proxy_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            f"Configured proxy at {proxy_url} is unavailable. "
            "Remote proxies are not started automatically."
        )
    parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "local_agent.proxy.app",
            "--host",
            listen_host,
            "--port",
            str(parsed_port),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Proxy exited while starting. Run local-agent-proxy for diagnostics.")
        if _healthy(proxy_url):
            return process
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError(f"Proxy did not become ready at {proxy_url}")


def launch(
    agent: str,
    project: Path,
    model: str,
    backend: str,
    trace: bool,
    proxy_url: str,
    extra_args: list[str],
    listen_host: str = "127.0.0.1",
) -> int:
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    ensure_proxy(proxy_url, listen_host=listen_host)
    with httpx.Client(timeout=10) as client:
        response = client.post(f"{proxy_url}/api/sessions", json={
            "client": agent, "project": project.name, "model": model,
            "backend": backend, "trace": trace, "tags": {"project_path": str(project)},
        })
        response.raise_for_status()
        session_id = response.json()["session_id"]
    env = os.environ.copy()
    env["AI_PROXY_SESSION"] = session_id
    if agent == "codex":
        env["CODEX_OSS_BASE_URL"] = f"{proxy_url}/session/{session_id}/v1"
        command = ["codex", "--oss", "-m", model, *extra_args]
    elif agent == "claude":
        env.update({"ANTHROPIC_BASE_URL": f"{proxy_url}/session/{session_id}/anthropic", "ANTHROPIC_AUTH_TOKEN": "local-ollama", "ANTHROPIC_API_KEY": ""})
        command = ["claude", "--model", model, *extra_args]
    else:
        raise ValueError(f"Unsupported agent: {agent}")
    print(f"Session : {session_id}\nAgent   : {agent}\nModel   : {model}\nBackend : {backend}")
    exit_status = 1
    try:
        exit_status = subprocess.run(command, cwd=project, env=env, check=False).returncode
    except FileNotFoundError:
        exit_status = 127
        print(f"Agent executable not found: {command[0]}", file=sys.stderr)
    finally:
        try:
            httpx.patch(f"{proxy_url}/api/sessions/{session_id}", json={"ended": True, "exit_status": exit_status}, timeout=10).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Warning: could not close proxy session: {exc}", file=sys.stderr)
    return exit_status


def _healthy(proxy_url: str) -> bool:
    try:
        return httpx.get(f"{proxy_url.rstrip('/')}/health", timeout=0.75).status_code == 200
    except httpx.HTTPError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="Launch a coding agent through the AI proxy")
    parser.add_argument("agent", choices=("codex", "claude"))
    parser.add_argument("project", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--backend", help="Override default_backend from config/backends.yaml")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--proxy", help="Override proxy.url from config/backends.yaml")
    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]
    try:
        settings = Settings.load()
        proxy_url = (args.proxy or settings.proxy_url).rstrip("/")
        backend = args.backend or settings.default_backend
        raise SystemExit(
            launch(
                args.agent,
                args.project,
                args.model,
                backend,
                args.trace,
                proxy_url,
                extra,
                listen_host=settings.host,
            )
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
