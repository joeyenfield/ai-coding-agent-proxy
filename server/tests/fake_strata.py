"""A stand-in for Strata's `strata` command in tests: saved profiles, installed models and their runtime state in a JSON file.

FAKE_STRATA_STATE names the file:

    {"profiles": {"<id>": "stopped" | "ready"},
     "configs": {"<id>": "<the run config the profile names>"},
     "models": [{"id": ..., "name": ..., "source_config": ..., "component_status": "present", "missing": []}],
     "no_models_list": true,           # play a Strata from before `models list --installed`
     "calls": [...]}

It answers `profile list`, `profile create <name> --from-config <file>`, `models list
--installed`, and `runtime status|start|stop --profile <id>`, with --json, as Strata's own
CLI does.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    path = Path(os.environ["FAKE_STRATA_STATE"])
    # agent-machine's minute update asks for status while a start runs: one call at a time keeps the file whole.
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
        state.setdefault("profiles", {})
        state.setdefault("configs", {})
        state.setdefault("calls", [])
        args = [arg for arg in sys.argv[1:] if arg != "--json"]
        state["calls"].append(args)
        answer, code = run(state, args)
        path.write_text(json.dumps(state, indent=2))
    finally:
        os.rmdir(lock)
    print(json.dumps({"schema_version": 1, **answer}))
    return code


def run(state: dict, args: list[str]) -> tuple[dict, int]:
    profiles, configs = state["profiles"], state["configs"]
    if args == ["profile", "list"]:
        listed = []
        for name in profiles:
            entry = {"id": name, "revision": "abc"}
            if name in configs:
                entry["source_config"] = configs[name]
            listed.append(entry)
        return {"ok": True, "profiles": listed}, 0
    if args == ["models", "list", "--installed"]:
        if state.get("no_models_list"):
            return {"ok": False, "error": "strata: error: argument: invalid choice: 'models'"}, 2
        return {"ok": True, "scope": "local_run_configs", "models": state.get("models", [])}, 0
    if len(args) == 5 and args[:2] == ["profile", "create"] and args[3] == "--from-config":
        name, config = args[2], args[4]
        if name in profiles:
            return {"ok": False, "error": {"code": 4, "message": f"profile {name} already exists"}}, 1
        if not Path(config).is_file():
            return {"ok": False, "error": {"code": 5, "message": "cannot read run config (FileNotFoundError)"}}, 1
        profiles[name], configs[name] = "stopped", config
        return {"ok": True, "profile": name}, 0
    if len(args) == 4 and args[0] == "runtime" and args[2] == "--profile":
        action, name = args[1], args[3]
        if name not in profiles:
            return {"ok": False, "error": f"no profile {name}"}, 1
        if action == "start":
            if name in configs and not Path(configs[name]).is_file():
                return {"ok": False, "error": {"code": 5, "message": "cannot read run config (FileNotFoundError)"}}, 1
            profiles[name] = "ready"
        elif action == "stop":
            profiles[name] = "stopped"
        elif action != "status":
            return {"ok": False, "error": f"unknown action {action}"}, 2
        answer = {"ok": True, "profile": name, "state": profiles[name]}
        if profiles[name] == "ready":
            answer["api_url"] = "http://127.0.0.1:18777"
        return answer, 0
    return {"ok": False, "error": f"fake strata: unsupported {args}"}, 2


if __name__ == "__main__":
    sys.exit(main())
