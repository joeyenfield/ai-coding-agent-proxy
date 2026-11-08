"""Keep canonical localhost OAuth addresses reachable in each container namespace.

The basic quickstart's addresses by default (the issuer at localhost:20000, the server at
localhost:19999); the peer quickstart (deploy/compose/peer/) runs two servers and names each one's
services and ports with QUICKSTART_AUTH_HOST/_PORT and QUICKSTART_SERVER_HOST/_PORT. A server can
also be given its organizations and people first (QUICKSTART_ORGS for their names, QUICKSTART_ACCOUNTS),
with QUICKSTART_PASSWORD. A machine relays its loopback 11434 to QUICKSTART_OLLAMA_HOST (default
ollama) with QUICKSTART_OLLAMA=1.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile

AUTH_HOST = os.getenv("QUICKSTART_AUTH_HOST") or "auth"
AUTH_PORT = int(os.getenv("QUICKSTART_AUTH_PORT") or 20000)
SERVER_HOST = os.getenv("QUICKSTART_SERVER_HOST") or "server"
SERVER_PORT = int(os.getenv("QUICKSTART_SERVER_PORT") or 19999)
OLLAMA_HOST = os.getenv("QUICKSTART_OLLAMA_HOST") or "ollama"


async def relay(reader, writer, host, port):
    upstream = None
    try:
        remote_reader, upstream = await asyncio.open_connection(host, port)

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
            if destination.can_write_eof():
                destination.write_eof()

        await asyncio.gather(copy(reader, upstream), copy(remote_reader, writer))
    except (OSError, ConnectionError):
        pass  # Callers retry normally while the other service starts or restarts.
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()


async def main(mode):
    listeners = [await asyncio.start_server(
        lambda r, w: relay(r, w, AUTH_HOST, 10000), "127.0.0.1", AUTH_PORT,
    )]
    if mode == "machine":
        listeners.append(await asyncio.start_server(
            lambda r, w: relay(r, w, SERVER_HOST, 9999), "127.0.0.1", SERVER_PORT,
        ))
        if os.getenv("QUICKSTART_OLLAMA") == "1":
            # agent-machine only discovers model servers on its own loopback.
            listeners.append(await asyncio.start_server(
                lambda r, w: relay(r, w, OLLAMA_HOST, 11434), "127.0.0.1", 11434,
            ))
        command = [sys.executable, "/quickstart/machine.py"]
    elif mode == "server":
        configure_vault(Path(os.getenv("AI_PROXY_LOG_DIR") or "/app/logs") / "private")
        add_orgs(os.getenv("QUICKSTART_ORGS") or "")
        add_accounts(os.getenv("QUICKSTART_ACCOUNTS") or "", os.environ.pop("QUICKSTART_PASSWORD", "") or "")
        command = ["agent-server"]
    else:
        raise ValueError("Expected server or machine")

    environment = dict(os.environ)
    # Only the bootstrap child needs the demo password; keep it out of this supervisor.
    os.environ.pop("QUICKSTART_PASSWORD", None)
    child = await asyncio.create_subprocess_exec(*command, env=environment)
    loop = asyncio.get_running_loop()

    def forward(number):
        if child.returncode is None:
            child.send_signal(number)

    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, forward, number)
    try:
        return await child.wait()
    finally:
        for listener in listeners:
            listener.close()
            await listener.wait_closed()


def configure_vault(directory: Path) -> None:
    """Provision a quickstart's encryption key once, outside its database, and reuse it on restart."""
    if os.getenv("AI_PROXY_VAULT_KEYS") or os.getenv("AI_PROXY_VAULT_KEYS_FILE"):
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "vault-keys"
    if not path.exists():
        # Publish a complete owner-only file without replacing a key another starter already made.
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            temporary.write("1:" + base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
        try:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                pass
            except OSError:
                # A volume without hardlinks (some network and overlay filesystems): create the file
                # exclusively, so a start racing this one can never replace the key it already made.
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as published:
                        published.write(temporary_path.read_bytes())
                        published.flush()
                        os.fsync(published.fileno())
        finally:
            temporary_path.unlink(missing_ok=True)
    if not path.read_text(encoding="ascii").strip():
        raise RuntimeError("The quickstart vault key file is empty; restore it from backup before starting")
    os.environ["AI_PROXY_VAULT_KEYS_FILE"] = str(path)


def account_arguments(entry: str) -> list[str]:
    """org:user[:role[:team+team...]], as the arguments of agent-server user add (teams are made as needed)."""
    fields = entry.split(":")
    org, user, role, teams = (fields + ["member", ""][len(fields) - 2:])[:4]
    return [
        "agent-server", "user", "add", user, "--org", org, *(["--admin"] if role == "admin" else []),
        *(argument for team in filter(None, teams.split("+")) for argument in ("--team", team)),
    ]


def add_orgs(orgs: str) -> None:
    """id=Display name,... made before the people, so each organization has its name. One that's there stays."""
    for entry in filter(None, (item.strip() for item in orgs.split(","))):
        org, _, name = entry.partition("=")
        report(f"organization {org}", subprocess.run(
            ["agent-server", "org", "add", org.strip(), *(["--name", name.strip()] if name.strip() else [])],
            capture_output=True, text=True,
        ))


def add_accounts(accounts: str, password: str) -> None:
    """org:user:role:teams,... as agent-server user add makes them (organizations and teams made as needed). An
    account that's there already stays as it is, so a restart changes nothing."""
    for entry in filter(None, (item.strip() for item in accounts.split(","))):
        org, user, role = (entry.split(":") + ["member"])[:3]
        result = subprocess.run(account_arguments(entry), env={**os.environ, "AI_PROXY_NEW_PASSWORD": password}, capture_output=True, text=True)
        report(f"{user} ({role} of {org})", result)


def report(what: str, result: subprocess.CompletedProcess) -> None:
    if result.returncode == 0:
        print(f"{what}: added", flush=True)
    else:
        lines = [line for line in (result.stderr or result.stdout).splitlines() if line.strip() and "Warning" not in line and "httpx2" not in line]
        print(f"{what}: {lines[-1] if lines else 'not added'}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
