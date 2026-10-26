"""A stand-in for systemctl and an ollama.service, for agent-machine's service tests.

  fake_systemctl.py show ollama -p LoadState --value   -> loaded
  fake_systemctl.py start|restart ollama               -> runs fake_ollama.py serve with the
                                                          environment file the drop-in names
  fake_systemctl.py stop ollama                        -> stops it

The service reads AI_PROXY_SERVICE_ENV (the EnvironmentFile), as the real drop-in makes
ollama.service do; FAKE_SERVICE_STATE remembers the running fake's process id.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def environment() -> dict[str, str]:
    values = {}
    for line in Path(os.environ["AI_PROXY_SERVICE_ENV"]).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name] = shlex.split(value)[0] if value else ""
    return values


def stop(state: Path) -> None:
    try:
        pid = json.loads(state.read_text())["pid"]
    except (OSError, ValueError, KeyError):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    state.unlink(missing_ok=True)
    time.sleep(0.5)


def main() -> int:
    args = sys.argv[1:]
    state = Path(os.environ.get("FAKE_SERVICE_STATE", "fake-service.json"))
    if args[:2] == ["show", "ollama"]:
        print("loaded")
        return 0
    if len(args) == 2 and args[1] == "ollama" and args[0] in {"start", "stop", "restart"}:
        if args[0] in {"stop", "restart"}:
            stop(state)
        if args[0] in {"start", "restart"}:
            env = {**os.environ, **environment()}
            fake = Path(__file__).with_name("fake_ollama.py")
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            process = subprocess.Popen([sys.executable, str(fake), "serve"], env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            state.write_text(json.dumps({"pid": process.pid, "env": environment()}))
        return 0
    print(f"fake systemctl: unsupported {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
