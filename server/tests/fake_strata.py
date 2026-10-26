"""A stand-in for Strata's `strata` command in tests: saved profiles and their runtime state in a JSON file.

FAKE_STRATA_STATE names the file: {"profiles": {"<id>": "stopped" | "ready"}, "calls": [...]}.
It answers `profile list`, and `runtime status|start|stop --profile <id>`, with --json,
as Strata's own CLI does.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    path = Path(os.environ["FAKE_STRATA_STATE"])
    state = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault("profiles", {})
    state.setdefault("calls", [])
    args = [arg for arg in sys.argv[1:] if arg != "--json"]
    state["calls"].append(args)
    answer, code = run(state, args)
    path.write_text(json.dumps(state, indent=2))
    print(json.dumps({"schema_version": 1, **answer}))
    return code


def run(state: dict, args: list[str]) -> tuple[dict, int]:
    profiles = state["profiles"]
    if args == ["profile", "list"]:
        return {"ok": True, "profiles": [{"id": name, "revision": "abc"} for name in profiles]}, 0
    if len(args) == 4 and args[0] == "runtime" and args[2] == "--profile":
        action, name = args[1], args[3]
        if name not in profiles:
            return {"ok": False, "error": f"no profile {name}"}, 1
        if action == "start":
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
