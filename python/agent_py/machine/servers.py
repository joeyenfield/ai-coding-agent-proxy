"""Starts and stops the Ollama installed on this machine when the server asks ({"type": "server"}
over the fleet connection), with the OLLAMA_* settings saved from the console. It's off until
`agent-machine servers on`. Mirrors machine/internal/servers in the Go agent-machine.

It only stops the Ollama it started itself (one run by its own app, a service or by hand is
left to that), and requests only name a kind, so the server can't run anything else here.
Starting Strata profiles and Ollama's systemd service are the Go agent-machine's only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import settings

# A command to run instead of ollama, as a JSON list of arguments (tests use a fake).
OLLAMA_ENV = "AI_PROXY_OLLAMA_COMMAND"
# Where Ollama listens by default, and where the fleet looks for it.
OLLAMA_PORT = 11434

MEMORY, SCHED, HARDWARE, SERVER = "Memory / KV cache", "Concurrency / scheduling", "Hardware", "Server"
GROUPS = [MEMORY, SCHED, HARDWARE, SERVER]
MAX_INT = 2**31 - 1


class Refused(Exception):
    pass


def setting(key: str, group: str, label: str, kind: str, default: Any, help: str, *env: str,
            choices: Optional[List[str]] = None, editable: bool = False, minimum: int = 0, maximum: int = MAX_INT, scale: int = 1) -> Dict[str, Any]:
    return {"key": key, "group": group, "label": label, "kind": kind, "default": default, "help": help, "env": list(env),
            "choices": choices or [], "editable": editable, "minimum": minimum, "maximum": maximum, "scale": scale}


# The table the console shows, in order: the same as the Go agent-machine's (ollama_settings.go).
SETTINGS = [
    setting("kv_cache_type", MEMORY, "KV cache type", "choice", "q4_0",
            "f16 = full precision, most memory. q8_0 ≈ 53% of f16. q4_0 ≈ 28% of f16. Quantized types require Flash Attention.",
            "OLLAMA_KV_CACHE_TYPE", choices=["f16", "q8_0", "q4_0"]),
    setting("flash_attention", MEMORY, "Flash Attention", "bool", True, "Required for a quantized KV cache.", "OLLAMA_FLASH_ATTENTION"),
    setting("context_length", MEMORY, "Context length (tokens)", "int", 16384,
            "8192 = small tasks, 16384 = recommended, 32768 = larger tasks, 65536 = large agent sessions.",
            "OLLAMA_CONTEXT_LENGTH", minimum=1024, maximum=1048576),
    setting("gpu_overhead_mb", MEMORY, "GPU overhead (MB)", "int", 0, "VRAM to reserve for the OS / other apps. 0 = no reservation.",
            "OLLAMA_GPU_OVERHEAD", minimum=0, maximum=1048576, scale=1024 * 1024),
    setting("num_parallel", SCHED, "Parallel requests", "int", 1, "1 = lowest KV cache memory. Each extra slot multiplies KV cache use.", "OLLAMA_NUM_PARALLEL", minimum=1, maximum=128),
    setting("max_loaded_models", SCHED, "Max loaded models", "int", 1, "Maximum number of models resident at once.", "OLLAMA_MAX_LOADED_MODELS", minimum=1, maximum=128),
    setting("max_queue", SCHED, "Max queued requests", "int", 512, "Requests beyond this are rejected while busy.", "OLLAMA_MAX_QUEUE", minimum=1, maximum=100000),
    setting("keep_alive", SCHED, "Keep alive", "choice", "-1", "-1 = keep loaded forever, 5m = unload after 5 min idle, 0 = unload immediately.",
            "OLLAMA_KEEP_ALIVE", choices=["-1", "0", "5m", "30m", "1h", "24h"], editable=True),
    setting("load_timeout", SCHED, "Load timeout", "choice", "10m", "How long to wait for a model to load before giving up.",
            "OLLAMA_LOAD_TIMEOUT", choices=["5m", "10m", "20m", "30m"], editable=True),
    setting("sched_spread", SCHED, "Spread across GPUs", "bool", False, "Always split models across all GPUs.", "OLLAMA_SCHED_SPREAD"),
    setting("multiuser_cache", SCHED, "Multi-user cache", "bool", False, "Optimise prompt caching for multiple concurrent users.", "OLLAMA_MULTIUSER_CACHE"),
    setting("noprune", SCHED, "Don't prune blobs", "bool", False, "Skip pruning unused model blobs on startup.", "OLLAMA_NOPRUNE"),
    setting("gpus", HARDWARE, "GPU IDs", "str", "", 'e.g. "0" or "0,1". Empty = all GPUs. "-1" = CPU only.', "CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"),
    setting("llm_library", HARDWARE, "LLM library", "choice", "", "Force a backend. Empty = automatic.", "OLLAMA_LLM_LIBRARY",
            choices=["", "cuda_v12", "cuda_v13", "rocm", "vulkan", "cpu"], editable=True),
    setting("vulkan", HARDWARE, "Vulkan backend", "bool", False, "Experimental Vulkan backend.", "OLLAMA_VULKAN"),
    setting("new_engine", HARDWARE, "New engine", "bool", False, "Opt into Ollama's new engine where supported.", "OLLAMA_NEW_ENGINE"),
    setting("models_dir", SERVER, "Models directory", "str", "", "Empty = standard model directory.", "OLLAMA_MODELS"),
    setting("origins", SERVER, "CORS origins", "str", "", "Comma-separated extra allowed origins.", "OLLAMA_ORIGINS"),
    setting("debug", SERVER, "Debug logging", "bool", False, "Verbose server log.", "OLLAMA_DEBUG"),
]


def schema() -> Dict[str, Any]:
    return {"groups": GROUPS, "settings": [{k: v for k, v in item.items() if k != "scale"} for item in SETTINGS]}


def default_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {"extra_env": ""}
    for item in SETTINGS:
        config[item["key"]] = item["default"]
    return config


def parse_extra_env(text: str) -> Dict[str, Optional[str]]:
    """KEY=VALUE lines; a blank value unsets the variable, '#' starts a comment. Only OLLAMA_* ones."""
    result: Dict[str, Optional[str]] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, found, value = line.partition("=")
        name = name.strip()
        if not found or not name or " " in name or "\t" in name:
            raise Refused(f"Extra environment line {number} is not KEY=VALUE: {raw.rstrip(chr(13))!r}")
        if not name.startswith("OLLAMA_") or name == "OLLAMA_HOST":
            raise Refused(f"Extra environment line {number} sets {name}: only OLLAMA_* variables (other than OLLAMA_HOST) can be set here")
        result[name] = value.strip() or None
    return result


def as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value != ""
    if value is None:
        return False
    number = as_int(value)
    return number is None or number != 0


def validate_config(data: Any) -> Dict[str, Any]:
    """Checks and tidies a config from the console against the table."""
    if not isinstance(data, dict):
        raise Refused("Configuration must be an object")
    config = default_config()
    for item in SETTINGS:
        key = item["key"]
        if key not in data:
            continue
        value = data[key]
        if item["kind"] == "bool":
            config[key] = truthy(value)
        elif item["kind"] == "int":
            number = as_int(value)
            if number is None:
                raise Refused(f"{item['label']} must be a number")
            if not item["minimum"] <= number <= item["maximum"]:
                raise Refused(f"{item['label']} must be between {item['minimum']} and {item['maximum']}")
            config[key] = number
        elif item["kind"] == "choice":
            text = as_text(value).strip()
            if not item["editable"] and item["choices"] and text not in item["choices"]:
                raise Refused(f"{item['label']} must be one of: {', '.join(item['choices'])}")
            config[key] = text
        else:
            config[key] = as_text(value).strip()
    config["extra_env"] = as_text(data.get("extra_env"))
    # A bad line is refused on saving rather than on starting.
    parse_extra_env(config["extra_env"])
    return config


def managed_env(config: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """The variables the config sets; None means unset."""
    env: Dict[str, Optional[str]] = {}
    for item in SETTINGS:
        value = config.get(item["key"], item["default"])
        if item["kind"] == "bool":
            text: Optional[str] = "1" if truthy(value) else "0"
        elif item["kind"] == "int":
            text = str((as_int(value) or 0) * item["scale"])
        else:
            text = as_text(value).strip() or None
        for name in item["env"]:
            env[name] = text
    try:
        env.update(parse_extra_env(as_text(config.get("extra_env"))))
    except Refused:
        pass
    return env


def environment(base: Dict[str, str], config: Dict[str, Any]) -> Dict[str, str]:
    """The environment Ollama starts with: base with the managed variables applied, and no OLLAMA_HOST."""
    managed = managed_env(config)
    result = {name: value for name, value in base.items() if name not in managed and name.upper() != "OLLAMA_HOST"}
    result.update({name: value for name, value in sorted(managed.items()) if value is not None})
    return result


def config_warnings(config: Dict[str, Any]) -> List[str]:
    warnings = []
    context = as_int(config.get("context_length")) or 0
    if context < 8192:
        warnings.append("Context below 8K is likely too small for coding agents.")
    elif context < 16384:
        warnings.append("Some coding agents may exhaust this context rapidly.")
    if (as_int(config.get("num_parallel")) or 0) > 1:
        warnings.append("Parallel requests increase KV cache memory requirements.")
    if config.get("kv_cache_type") != "f16" and not truthy(config.get("flash_attention")):
        warnings.append("A quantized KV cache requires Flash Attention; enable it.")
    return warnings


def config_path() -> Path:
    return settings.home() / "ollama.json"


def log_path() -> Path:
    return settings.home() / "ollama.log"


def state_path() -> Path:
    return settings.home() / "servers.json"


def load_config() -> Dict[str, Any]:
    """The saved config over the defaults; a missing or broken file is the defaults."""
    try:
        return validate_config(json.loads(config_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, Refused):
        return default_config()


def save_config(data: Any) -> Dict[str, Any]:
    config = validate_config(data)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return config


def load_state() -> Dict[str, Any]:
    """servers.json in agent-machine's folder: the Ollama it started (ollama_pid, ollama_bind, ollama_env)."""
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(value: Dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def managed_pid() -> int:
    """The Ollama this agent-machine started, while it still runs."""
    pid = int(load_state().get("ollama_pid") or 0)
    return pid if pid > 0 and settings.alive(pid) else 0


def ollama_command() -> Optional[List[str]]:
    """ollama on PATH, or where its installer (or Termux's package) puts it."""
    override = os.environ.get(OLLAMA_ENV)
    if override:
        try:
            parts = json.loads(override)
        except ValueError:
            return None
        return [str(part) for part in parts] if isinstance(parts, list) and parts else None
    found = shutil.which("ollama")
    if found:
        return [found]
    if sys.platform == "win32":
        candidates = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"]
    else:
        prefix = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
        candidates = ["/usr/local/bin/ollama", "/usr/bin/ollama", os.path.join(prefix, "bin", "ollama")]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return [candidate]
    return None


def stop_tree(pid: int) -> None:
    """Stops the process and what it started (Ollama runs its model runners as children)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    try:
        # It was started in a session of its own, so its group is its tree.
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def tail(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "no log"
    return "…" + text[-limit:] if len(text) > limit else text


class Manager:
    """Answers the server's server requests. machine reads the machine's settings fresh for each one."""

    def __init__(self, machine: Callable[[], settings.Machine] = settings.Machine.load, port: Optional[int] = None) -> None:
        self.machine = machine
        self._port = port
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        # Read when used, so tests can point the module at another port.
        return self._port or OLLAMA_PORT

    def describe(self) -> Dict[str, Any]:
        """The "servers" part of the machine's hello: what's installed and running."""
        if not self.machine().servers:
            return {"enabled": False}
        return {"enabled": True, "ollama": self.ollama_state(), "strata": {"found": False, "profiles": []}}

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """One request, always answered with a server_result for its id."""
        with self.lock:
            answer: Dict[str, Any] = {"type": "server_result", "request": request.get("request")}
            try:
                self._handle(request)
                answer["ok"] = True
            except Refused as error:
                answer.update(ok=False, error=str(error))
            except Exception as error:  # noqa: BLE001 - a failed start never takes the connection down
                answer.update(ok=False, error=f"{type(error).__name__}: {error}")
            if request.get("kind") == "ollama" and request.get("op") in ("settings", "configure") and self.machine().servers:
                answer["ollama_settings"] = self.ollama_settings()
            answer["servers"] = self.describe()
            return answer

    def _handle(self, request: Dict[str, Any]) -> None:
        if not self.machine().servers:
            raise Refused("Starting model servers is off on this machine (agent-machine servers on)")
        op, kind = request.get("op"), request.get("kind")
        if op == "status" or (kind == "ollama" and op == "settings"):
            return
        if kind == "ollama" and op == "configure":
            save_config(request.get("config"))
        elif kind == "ollama" and op in ("start", "restart"):
            bind = str(request.get("bind") or "127.0.0.1")
            if bind not in ("127.0.0.1", "0.0.0.0"):
                raise Refused(f"Ollama listens on 127.0.0.1 or 0.0.0.0, not {bind!r}")
            if op == "restart":
                self.stop_ollama()
            self.start_ollama(bind)
        elif kind == "ollama" and op == "stop":
            self.stop_ollama()
        elif kind == "strata":
            raise Refused("Starting Strata is only in the Go agent-machine")
        else:
            raise Refused(f"unknown request: {op} {kind}")

    def ollama_version(self) -> str:
        """The version of the Ollama answering on the port, or "" when none does."""
        try:
            with httpx.Client(trust_env=False, timeout=1.5) as http:
                response = http.get(f"http://127.0.0.1:{self.port}/api/version")
            if response.status_code != 200:
                return ""
            return str(response.json().get("version") or "unknown")
        except (httpx.HTTPError, ValueError, AttributeError):
            return ""

    def ollama_state(self) -> Dict[str, Any]:
        version = self.ollama_version()
        pid = managed_pid()
        result: Dict[str, Any] = {"found": ollama_command() is not None, "running": bool(version), "version": version, "managed": pid > 0, "port": self.port}
        if pid:
            result["bind"] = load_state().get("ollama_bind")
        return result

    def ollama_settings(self) -> Dict[str, Any]:
        config = load_config()
        env = managed_env(config)
        matches = None
        state = load_state()
        if managed_pid() and self.ollama_version() and isinstance(state.get("ollama_env"), dict):
            matches = state["ollama_env"] == env
        return {"schema": schema(), "config": config, "env": env, "warnings": config_warnings(config),
                "applied_matches_saved": matches, "config_path": str(config_path()), "log_path": str(log_path())}

    def start_ollama(self, bind: str) -> None:
        if self.ollama_version():
            return
        program = ollama_command()
        if program is None:
            raise Refused("Ollama isn't installed here (https://ollama.com/download; in Termux, pkg install ollama), or isn't on PATH")
        config = load_config()
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as log:
            log.write(f"# agent-machine: starting {' '.join(program)} serve on {bind}:{self.port} at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
            for name, value in sorted(managed_env(config).items()):
                log.write(f"#   {name}={'(unset)' if value is None else value}\n")
            log.flush()
            # The saved settings, then OLLAMA_HOST, which is where it listens: loopback for the server
            # on this computer or a machine that tunnels, else every address, so the server can reach it.
            env = {**environment(dict(os.environ), config), "OLLAMA_HOST": f"{bind}:{self.port}"}
            flags: Dict[str, Any] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                if sys.platform == "win32" else {"start_new_session": True})
            try:
                process = subprocess.Popen([*program, "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **flags)
            except OSError as error:
                raise Refused(f"couldn't start Ollama: {error}")
        # Reaped here, so a stopped Ollama doesn't linger as a zombie that still looks alive.
        threading.Thread(target=process.wait, name="ollama-wait", daemon=True).start()
        save_state({"ollama_pid": process.pid, "ollama_bind": bind, "ollama_env": managed_env(config)})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.ollama_version():
                return
            if process.poll() is not None:
                raise Refused(f"Ollama exited while starting: {tail(path, 600)}")
            time.sleep(0.25)
        raise Refused(f"Ollama started but doesn't answer yet; see {path}")

    def stop_ollama(self) -> None:
        pid = managed_pid()
        if not pid:
            if self.ollama_version():
                raise Refused("This Ollama was started outside agent-machine (by hand, or by its own app), so agent-machine can't stop it; "
                              "stop it where it runs, then start it from the console")
            return
        stop_tree(pid)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (settings.alive(pid) or self.ollama_version()):
            time.sleep(0.2)
        if settings.alive(pid) and sys.platform != "win32":
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        save_state({})
        if self.ollama_version():
            raise Refused("Ollama is still answering after being stopped")
