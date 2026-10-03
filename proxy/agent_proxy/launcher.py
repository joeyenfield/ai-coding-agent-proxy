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

from .agents import AgentDefinition, load_agents
from .config import Settings


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
            "agent_proxy.app",
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
    agent_id: str,
    project: Path,
    model: str,
    backend: str,
    trace: bool,
    proxy_url: str,
    extra_args: list[str],
    listen_host: str = "127.0.0.1",
    agents: dict[str, AgentDefinition] | None = None,
) -> int:
    agents = agents if agents is not None else load_agents(Settings.load().agents_file)
    agent = agents.get(agent_id)
    if not agent:
        raise ValueError(f"Unknown agent '{agent_id}'. Available: {', '.join(sorted(agents)) or 'none'}")
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    executable = agent.installed_path()
    if not executable:
        hint = f" Install it with: {agent.install}" if agent.install else ""
        raise ValueError(f"{agent.name} ({agent.executable}) is not on PATH.{hint}")
    ensure_proxy(proxy_url, listen_host=listen_host)
    with httpx.Client(timeout=10) as client:
        response = client.post(f"{proxy_url}/api/sessions", json={
            "client": agent.id, "project": project.name, "model": model,
            "backend": backend, "trace": trace, "tags": {"project_path": str(project)},
        })
        response.raise_for_status()
        session_id = response.json()["session_id"]
    rendered = agent.render(proxy_url, session_id, model, str(project))
    env = {**os.environ, **rendered["env"]}
    # Resolve through PATH so npm .cmd shims work on Windows.
    command = [executable, *rendered["command"][1:], *extra_args]
    print(f"Session : {session_id}\nAgent   : {agent.name}\nModel   : {model}\nBackend : {backend}\nUI      : {proxy_url}", flush=True)
    exit_status = 1
    try:
        exit_status = subprocess.run(command, cwd=project, env=env, check=False).returncode
    except FileNotFoundError:
        exit_status = 127
        print(f"Agent executable not found: {command[0]}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_status = 130
    finally:
        try:
            httpx.patch(f"{proxy_url}/api/sessions/{session_id}", json={"ended": True, "exit_status": exit_status}, timeout=10).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Warning: could not close proxy session: {exc}", file=sys.stderr)
    return exit_status


def print_agents(agents: dict[str, AgentDefinition]) -> None:
    width = max((len(agent_id) for agent_id in agents), default=5)
    for agent_id, agent in agents.items():
        state = "installed" if agent.installed_path() else "missing  "
        print(f"{agent_id:<{width}}  {state}  {agent.name} ({agent.protocol})")
        if not agent.installed_path() and agent.install:
            print(f"{'':<{width}}             install: {agent.install}")


def _healthy(proxy_url: str) -> bool:
    try:
        return httpx.get(f"{proxy_url.rstrip('/')}/health", timeout=0.75).status_code == 200
    except httpx.HTTPError:
        return False


def main() -> None:
    settings = Settings.load()
    agents = load_agents(settings.agents_file)
    parser = argparse.ArgumentParser(prog="agent", description="Launch a coding agent through the AI proxy")
    parser.add_argument("agent", nargs="?", help=f"One of: {', '.join(agents)}")
    parser.add_argument("project", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--model", "-m", help="Model name or alias from config/models.yaml")
    parser.add_argument("--backend", help="Override default_backend from config/backends.yaml")
    parser.add_argument("--trace", action="store_true", help="Capture full request and response payloads")
    parser.add_argument("--proxy", help="Override proxy.url from config/backends.yaml")
    parser.add_argument("--list", action="store_true", help="List configured agents and whether they are installed")
    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]
    if args.list or not args.agent:
        print_agents(agents)
        raise SystemExit(0 if args.list else 2)
    if not args.model:
        parser.error("--model is required")
    try:
        proxy_url = (args.proxy or settings.proxy_url).rstrip("/")
        backend = args.backend or settings.default_backend
        raise SystemExit(launch(
            args.agent, args.project, args.model, backend, args.trace, proxy_url, extra,
            listen_host=settings.host, agents=agents,
        ))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
