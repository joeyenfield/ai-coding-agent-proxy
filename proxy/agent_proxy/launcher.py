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
from .certs import write_bundle
from .config import DIRECT_BACKEND, Settings


def ensure_proxy(
    proxy_url: str,
    listen_host: str = "127.0.0.1",
    # Generous because a fresh proxy rebuilds the web UI first when its sources changed.
    timeout: float = 120,
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
    model: str | None,
    backend: str | None,
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
    if agent.needs_model and not model:
        raise ValueError(f"{agent.name} needs --model")
    ensure_proxy(proxy_url, listen_host=listen_host)
    intercept = None
    with httpx.Client(timeout=10) as client:
        if agent.intercept:
            intercept = _intercept(client, proxy_url)
            backend = DIRECT_BACKEND
        else:
            # Pick a backend of the type this agent works with when none was given.
            backend = Settings.load().backend_for(agent.backend_type, backend).name
        response = client.post(f"{proxy_url}/api/sessions", json={
            "client": agent.id, "project": project.name, "model": model or None,
            "backend": backend, "trace": trace, "tags": {"project_path": str(project)},
        })
        if response.status_code >= 400:
            raise ValueError(_detail(response))
        session_id = response.json()["session_id"]
    rendered = agent.render(proxy_url, session_id, model or "", str(project), intercept=intercept)
    env = {**os.environ, **rendered["env"]}
    # Resolve through PATH so npm .cmd shims work on Windows.
    command = [executable, *rendered["command"][1:], *extra_args]
    route = f"all HTTPS through {intercept['url']} (traffic goes to the agent's own service)" if intercept else backend
    print(f"Session : {session_id}\nAgent   : {agent.name}\nModel   : {model or 'agent default'}\nBackend : {route}\nUI      : {proxy_url}/live", flush=True)
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


def _intercept(client: httpx.Client, proxy_url: str) -> dict[str, Any]:
    """Find the intercept proxy and make sure its CA is readable on this machine."""
    info = client.get(f"{proxy_url}/api/intercept").raise_for_status().json()
    if not info.get("running"):
        raise RuntimeError(f"The proxy's HTTPS intercept listener isn't running: {info.get('error') or 'unknown error'}")
    if not Path(info["ca_path"]).is_file():
        # The proxy runs on another machine; fetch its CA and build a local bundle.
        pem = client.get(f"{proxy_url}/api/intercept/ca.pem").raise_for_status().text
        directory = Path.home() / ".cache" / "agent-proxy" / str(info["ca_fingerprint"]).replace(":", "")[:16]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ca.pem").write_text(pem, encoding="ascii")
        write_bundle(pem, directory / "bundle.pem")
        info.update(ca_path=str(directory / "ca.pem"), bundle_path=str(directory / "bundle.pem"))
    return info


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail") or response.text)
    except ValueError:
        return response.text


def print_agents(agents: dict[str, AgentDefinition]) -> None:
    width = max((len(agent_id) for agent_id in agents), default=5)
    for agent_id, agent in agents.items():
        state = "installed" if agent.installed_path() else "missing  "
        route = "account, captured via HTTPS intercept" if agent.intercept else agent.protocol
        print(f"{agent_id:<{width}}  {state}  {agent.name} ({route})")
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
    parser.add_argument("--model", "-m", help="Model name or alias from config/models.yaml (optional for account agents)")
    parser.add_argument("--backend", help="Backend from config/backends.yaml (default: default_backend, or the first backend of the type the agent needs)")
    parser.add_argument("--trace", action="store_true", help="Capture full request and response payloads")
    parser.add_argument("--proxy", help="Override proxy.url from config/backends.yaml")
    parser.add_argument("--list", action="store_true", help="List configured agents and whether they are installed")
    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]
    if args.list or not args.agent:
        print_agents(agents)
        raise SystemExit(0 if args.list else 2)
    agent = agents.get(args.agent)
    if agent and agent.needs_model and not args.model:
        parser.error(f"--model is required for {agent.name}")
    try:
        proxy_url = (args.proxy or settings.proxy_url).rstrip("/")
        backend = args.backend
        raise SystemExit(launch(
            args.agent, args.project, args.model, backend, args.trace, proxy_url, extra,
            listen_host=settings.host, agents=agents,
        ))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
