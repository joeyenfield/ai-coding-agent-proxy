"""A stand-in for the docker CLI in tests: keeps containers in a JSON file and records every call.

FAKE_DOCKER_STATE names the file. Set "down": true in it to act like a stopped engine.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    path = Path(os.environ["FAKE_DOCKER_STATE"])
    # The proxy polls inspect while a start or stop runs; one call at a time keeps the file whole.
    lock = path.with_suffix(".lock")
    deadline = time.monotonic() + 10
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
        state.setdefault("containers", {})
        state.setdefault("images", [])
        state.setdefault("calls", [])
        args = sys.argv[1:]
        state["calls"].append(args)
        code = run(state, args)
        path.write_text(json.dumps(state, indent=2))
        return code
    finally:
        os.rmdir(lock)


def run(state: dict, args: list[str]) -> int:
    containers = state["containers"]
    command = args[0]
    if command == "version":
        if state.get("down"):
            print(json.dumps({"Client": {"Version": "27.3.1"}, "Server": None}))
            print("Cannot connect to the Docker daemon. Is the docker daemon running?", file=sys.stderr)
            return 1
        print(json.dumps({"Client": {"Version": "27.3.1"}, "Server": {"Version": "27.3.1"}}))
        return 0
    if command == "image" and args[1] == "inspect":
        return 0 if args[2] in state["images"] else 1
    if command == "pull":
        if "missing" in args[1]:
            print(f"Error response from daemon: manifest for {args[1]} not found")
            return 1
        print(f"latest-cuda13: Pulling from {args[1].split(':')[0]}")
        print("Digest: sha256:abc123")
        print(f"Downloaded newer image for {args[1]}")
        state["images"].append(args[1])
        return 0
    if command == "run":
        options = parse_run(args[1:])
        name = options["name"]
        if name in containers:
            print(f'Conflict. The container name "/{name}" is already in use', file=sys.stderr)
            return 125
        containers[name] = {**options, "running": True, "exit_code": 0, "health": "starting" if "strata" in options["image"] else None}
        print("f00dbabe" * 8)
        return 0
    if command == "start":
        containers[args[1]]["running"] = True
        return 0
    if command == "stop":
        name = args[-1]
        if name not in containers:
            print(f"Error response from daemon: No such container: {name}", file=sys.stderr)
            return 1
        containers[name]["running"] = False
        return 0
    if command == "rm":
        name = args[-1]
        if name not in containers:
            print(f"Error: No such container: {name}", file=sys.stderr)
            return 1
        del containers[name]
        return 0
    if command == "inspect":
        found, missing = [], []
        for name in args[1:]:
            if name in containers:
                found.append(inspect(name, containers[name]))
            else:
                missing.append(name)
        print(json.dumps(found))
        for name in missing:
            print(f"Error: No such object: {name}", file=sys.stderr)
        return 1 if missing else 0
    if command == "logs":
        container = containers.get(args[-1])
        if not container:
            print(f"Error: No such container: {args[-1]}", file=sys.stderr)
            return 1
        print(f"strata: serving on :8080 with key {container['env'].get('API_KEY', '-')}")
        print("strata: model loaded", file=sys.stderr)
        return 0
    print(f"fake docker: unsupported {args}", file=sys.stderr)
    return 2


def parse_run(args: list[str]) -> dict:
    options: dict = {"labels": {}, "env": {}, "ports": [], "volumes": [], "flags": []}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-d":
            index += 1
            continue
        if arg in {"--name", "--label", "-e", "-p", "-v"}:
            value = args[index + 1]
            if arg == "--name":
                options["name"] = value
            elif arg == "--label":
                key, _, label = value.partition("=")
                options["labels"][key] = label
            elif arg == "-e":
                key, _, env = value.partition("=")
                options["env"][key] = env
            elif arg == "-p":
                options["ports"].append(value)
            else:
                options["volumes"].append(value)
            index += 2
            continue
        if arg.startswith("-"):
            options["flags"] += [arg, args[index + 1]]
            index += 2
            continue
        options["image"] = arg
        index += 1
    return options


def inspect(name: str, container: dict) -> dict:
    status = "running" if container["running"] else "exited"
    state = {"Status": status, "Running": container["running"], "ExitCode": container["exit_code"],
             "StartedAt": "2026-10-25T15:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z", "Error": ""}
    if container["health"]:
        state["Health"] = {"Status": container["health"]}
    return {"Name": f"/{name}", "State": state, "Config": {"Labels": container["labels"], "Image": container["image"]}}


if __name__ == "__main__":
    sys.exit(main())
