"""The all-in-one image's first process (devhelpers/allinone/Dockerfile): the whole system in one container.

1. agent-server and agent-auth start as `agent-server local` does (server/agent_server/local.py),
   with everything in /data, listening on every address of the container so the host's published
   ports reach them. Their addresses stay http://localhost:19999 and :20000, which the browser on
   the host and the processes in here both reach.
2. On the first start, agent-machine and agent sign in as admin through the issuer's device flow
   (deploy/compose/quickstart/machine.py's sign_in, as a browser would), with the first password: the one the
   server made (printed below) or MESH_ADMIN_PASSWORD. Later starts reuse those sign-ins.
3. This container becomes a machine of its own fleet: launches, files, terminals and model servers
   on, for /workspace. Then agent-machine runs in the foreground.

It stays up while all three run; when one stops, or the container is stopped, it stops the others.
"""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_server import local  # noqa: E402
from quickstart_machine import sign_in  # noqa: E402

CONFIG = Path.home() / ".config"
WORKSPACE = "/workspace"


def say(text: str) -> None:
    print(text, flush=True)


def first_password(instance: local.Instance, chosen: str) -> str:
    if chosen:
        return chosen
    try:
        return instance.password_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def install_packages(packages: str) -> None:
    """AGENT_APT_PACKAGES, as agent-sandbox's entrypoint: the ones missing, with the image's sudo.
    A failure is said and skipped: the system starts without them."""
    missing = [name for name in packages.split() if subprocess.run(["dpkg", "-s", name], capture_output=True).returncode != 0]
    if not missing:
        return
    say(f"Installing {' '.join(missing)}...")
    update = subprocess.run(["sudo", "-n", "apt-get", "update", "-qq"], capture_output=True)
    install = update.returncode == 0 and subprocess.run(
        ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing],
        capture_output=True).returncode == 0
    if not install:
        say(f"Couldn't install {' '.join(missing)} (AGENT_APT_PACKAGES needs the image's sudo); going on without them.")


def main() -> int:
    install_packages(os.environ.get("AGENT_APT_PACKAGES", ""))
    chosen = os.environ.pop("MESH_ADMIN_PASSWORD", "")
    instance = local.Instance.load(local.instance_home())
    overrides = {"AI_PROXY_HOST": "0.0.0.0", "AI_PROXY_AUTH_HOST": "0.0.0.0", "AI_PROXY_UI_BUILD": "0"}
    if chosen:
        # admin with this password comes from the environment, for the server and the issuer alike, as in the quickstart.
        overrides.update(AI_PROXY_USERNAME="admin", AI_PROXY_PASSWORD=chosen)
    say("Starting agent-server and agent-auth...")
    quiet = io.StringIO()
    if local.start(instance, out=quiet, overrides=overrides) != 0:
        say(quiet.getvalue())
        return 1
    instance = local.Instance.load(instance.home)

    password = first_password(instance, chosen)
    signed = all((CONFIG / name / "credentials.json").is_file() for name in ("agent-machine", "agent-proxy"))
    if not signed and not password:
        say("This machine has no sign-in yet and the first password is gone: set MESH_ADMIN_PASSWORD to admin's password.")
        local.stop(instance, quiet=True)
        return 1
    try:
        sign_in(CONFIG / "agent-machine" / "credentials.json", password)
        sign_in(CONFIG / "agent-proxy" / "credentials.json", password)
    except Exception as error:  # a changed password, say: the server stays up to fix it from the console
        say(f"Couldn't sign this machine in as admin ({error}). Set MESH_ADMIN_PASSWORD to admin's password and restart.")
    name = socket.gethostname()
    for arguments in (
        ["name", name], ["launches", "on"], ["files", "on"], ["commands", "on"], ["servers", "on"],
        ["containers", "off"], ["roots", "add", WORKSPACE],
    ):
        subprocess.run(["agent-machine", *arguments], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    say("")
    say("AI Control Mesh is running.")
    say(f"  Console: {instance.url}")
    if password and not chosen:
        say(f"  Sign in as admin with the password {password}")
        say(f"  (kept in {instance.password_path} until you change it in the console and delete that file)")
    elif chosen:
        say("  Sign in as admin with MESH_ADMIN_PASSWORD")
    say(f"  This container is a machine of its fleet: start agents in {WORKSPACE} from the console.")
    say("")

    machine = subprocess.Popen(["agent-machine", "run"])
    stopping = []

    def leave(number: int, _frame: object) -> None:
        stopping.append(number)

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, leave)
    code = 0
    while not stopping:
        if machine.poll() is not None:
            say(f"agent-machine stopped (exit {machine.returncode}).")
            code = machine.returncode or 1
            break
        instance = local.Instance.load(instance.home)
        if not all(local.running(instance.processes.get(program)) for program in local.PROGRAMS):
            say(f"agent-server or agent-auth stopped: see {instance.home / 'logs'}.")
            code = 1
            break
        time.sleep(2)
    if machine.poll() is None:
        machine.terminate()
        try:
            machine.wait(timeout=15)
        except subprocess.TimeoutExpired:
            machine.kill()
    local.stop(local.Instance.load(instance.home), quiet=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
