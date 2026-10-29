"""Answers the server's questions about the model servers on this machine ({"type": "provider"}
over the fleet connection): their state, their models (pull, delete, load, unload), their
logs, and this machine's load.

The server never talks to a model server to manage it; it asks the machine that runs it.
Requests only name a kind, a loopback port and a model, and each op calls fixed paths on
127.0.0.1:<port>, so the server can't reach anything else through here. Reading is always
allowed; changing models needs `agent-machine servers on`.

Ollama is covered as fully as in the Go agent-machine. Docker Model Runner and Strata report
their state and models, and Model Runner's models can be changed; Strata's load and unload,
its logs and the catalogue searches are the Go agent-machine's only.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from . import settings

Progress = Callable[[Dict[str, Any]], None]

KINDS = {"ollama": "Ollama", "strata": "Strata", "docker": "Docker Model Runner"}
CHANGES = {"pull", "delete", "load", "unload"}
OLLAMA_PORT = 11434

# Where each kind of model server listens by default, and the path that says it's there.
PROBES = (("ollama", "/api/version", 11434), ("docker", "/engines/v1/models", 12434), ("strata", "/v1/models", 18500))


class Refused(Exception):
    pass


def local() -> httpx.Client:
    """Model servers get a plain client, without the server's headers: they're not the server's business."""
    return httpx.Client(trust_env=False, timeout=None)


def error_text(response: httpx.Response, who: str) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        value = body.get("error") or body.get("message")
        if isinstance(value, dict):
            value = value.get("message")
        if value:
            return str(value)
    return response.text.strip() or f"{who} returned {response.status_code}"


def call(method: str, url: str, who: str, body: Any = None, headers: Optional[Dict[str, str]] = None, timeout: float = 60) -> Any:
    try:
        with local() as http:
            response = http.request(method, url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as error:
        raise Refused(f"{who} unavailable: {error}")
    if response.status_code >= 400:
        raise Refused(error_text(response, who))
    try:
        return response.json()
    except ValueError:
        return None


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 5) -> Tuple[Any, int]:
    with local() as http:
        response = http.get(url, headers=headers, timeout=timeout)
    try:
        return response.json(), response.status_code
    except ValueError:
        return None, response.status_code


def stream(url: str, body: Any, who: str, line: Callable[[Dict[str, Any]], None], headers: Optional[Dict[str, str]] = None) -> None:
    """Calls line for each JSON line of a streamed answer."""
    try:
        with local() as http, http.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                response.read()
                raise Refused(error_text(response, who))
            for text in response.iter_lines():
                text = text.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    line(value)
    except httpx.HTTPError as error:
        raise Refused(f"{who} unavailable: {error}")


class Manager:
    """Answers provider requests. machine reads the machine's settings fresh for each request."""

    def __init__(self, machine: Callable[[], settings.Machine] = settings.Machine.load, host: str = "127.0.0.1") -> None:
        self.machine = machine
        self.host = host

    def handle(self, request: Dict[str, Any], progress: Optional[Progress] = None) -> Dict[str, Any]:
        """One request, always answered with a provider_result for its id."""
        try:
            answer = self._handle(request, progress or (lambda update: None)) or {}
            answer["ok"] = True
        except Refused as error:
            answer = {"ok": False, "error": str(error)}
        except Exception as error:  # a bad model server never takes the connection down
            answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        answer["type"], answer["request"] = "provider_result", request.get("request")
        return answer

    def _handle(self, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        op = str(request.get("op") or "")
        if op == "metrics":
            return {"metrics": metrics()}
        if op == "search":
            raise Refused("Searching the model catalogues is only in the Go agent-machine")
        kind = str(request.get("kind") or "")
        if kind not in KINDS:
            raise Refused(f"unknown model server kind {kind!r}")
        port = request.get("port")
        if not isinstance(port, int) or not 0 < port <= 65535:
            raise Refused("name the model server's port")
        if op in CHANGES and not self.machine().servers:
            raise Refused("Managing model servers is off on this machine (agent-machine servers on)")
        base = f"http://{self.host}:{port}"
        model = str(request.get("model") or "").strip()
        if op in CHANGES and kind != "strata" and not model:
            raise Refused("A model name is required")
        handler = getattr(self, f"{kind}_{op}", None)
        if handler is None:
            raise Refused(f"{KINDS[kind]} can't {op}")
        return handler(base, model, request, progress)

    # Ollama

    def ollama_status(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        return {"state": ollama_status(base)}

    def ollama_pull(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        failed: List[str] = []

        def line(update: Dict[str, Any]) -> None:
            if update.get("error"):
                failed.append(str(update["error"]))
                update["status"] = "error"
            progress(update)

        stream(base + "/api/pull", {"model": model, "stream": True}, "Ollama", line)
        if failed:
            raise Refused(failed[0])
        return {}

    def ollama_delete(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        call("DELETE", base + "/api/delete", "Ollama", {"model": model})
        return {"result": {"deleted": model}}

    def ollama_load(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        call("POST", base + "/api/generate", "Ollama", {"model": model, "stream": False}, timeout=600)
        return {"result": {"loaded": model}}

    def ollama_unload(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        call("POST", base + "/api/generate", "Ollama", {"model": model, "keep_alive": 0, "stream": False})
        return {"result": {"unloaded": model}}

    def ollama_log(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        if request.get("port") != OLLAMA_PORT:
            raise Refused("Only the Ollama agent-machine starts (on port 11434) has a log here")
        path = settings.home() / "ollama.log"
        if not path.exists():
            return {"text": "No log here yet: agent-machine keeps a log only of the Ollama it starts (from the console, with agent-machine servers on)."}
        return {"text": tail(path, int(request.get("tail") or 0))}

    # Docker Model Runner

    def docker_status(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        return {"state": docker_status(base)}

    def docker_pull(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        failed: List[str] = []

        def line(message: Dict[str, Any]) -> None:
            kind = str(message.get("type") or "")
            if kind == "error":
                failed.append(str(message.get("message") or "the pull failed"))
                progress({"status": "error", "error": failed[-1]})
            elif kind == "success":
                progress({"status": "success"})
            else:
                update: Dict[str, Any] = {"status": str(message.get("message") or "pulling")}
                total, pulled = message.get("total"), message.get("pulled")
                if isinstance(total, (int, float)) and isinstance(pulled, (int, float)):
                    update["total"], update["completed"] = total, pulled
                progress(update)

        stream(base + "/models/create", {"from": model}, "Model Runner", line, headers={"Accept": "application/json"})
        if failed:
            raise Refused(failed[0])
        return {}

    def docker_delete(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        call("DELETE", base + "/models/" + model_path(model), "Docker Model Runner")
        return {"result": {"deleted": model}}

    def docker_load(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        call("POST", base + "/engines/v1/chat/completions", "Docker Model Runner", {"model": model, "messages": []}, headers={"X-Preload-Only": "true"}, timeout=600)
        return {"result": {"loaded": model}}

    def docker_unload(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        answer = call("POST", base + "/engines/unload", "Docker Model Runner", {"models": [model]})
        return {"result": {"unloaded": model, "runners": (answer or {}).get("unloaded_runners") if isinstance(answer, dict) else None}}

    # Strata

    def strata_status(self, base: str, model: str, request: Dict[str, Any], progress: Progress) -> Dict[str, Any]:
        key = str(request.get("key") or "")
        headers = {"Authorization": f"Bearer {key}"} if key else None
        unknown = {"state": "unknown", "source": "server" if key else "none"}
        try:
            listed, status = get_json(base + "/v1/models", headers)
        except httpx.HTTPError as error:
            return {"state": {"online": False, "error": str(error), "key": unknown, "models": []}}
        if status == 401:
            return {"state": {"online": True, "error": "Strata wants its API key.", "key": {"state": "rejected" if key else "missing", "source": unknown["source"]}, "models": []}}
        if status >= 400 or not isinstance(listed, dict):
            return {"state": {"online": False, "error": f"Strata returned {status}", "key": unknown, "models": []}}
        models = [{"name": str(item.get("id")), "loaded": True} for item in listed.get("data") or [] if isinstance(item, dict)]
        return {"state": {"online": True, "key": {"state": "accepted" if key else "none", "source": unknown["source"]}, "models": models}}


def ollama_status(base: str) -> Dict[str, Any]:
    """The Ollama's models, from /api/tags, with what /api/ps says is loaded."""
    try:
        tags, status = get_json(base + "/api/tags")
    except httpx.HTTPError as error:
        return {"online": False, "error": str(error), "models": []}
    if status >= 400:
        return {"online": False, "error": f"Ollama returned {status}", "models": []}
    try:
        running, _ = get_json(base + "/api/ps")
        version, _ = get_json(base + "/api/version")
    except httpx.HTTPError:
        running, version = None, None
    loaded = {str(item.get("name")): item for item in (running or {}).get("models") or [] if isinstance(item, dict)}
    models = []
    for item in (tags or {}).get("models") or []:
        if not isinstance(item, dict):
            continue
        details = item.get("details") or {}
        name = str(item.get("name") or "")
        model: Dict[str, Any] = {
            "name": name, "size": item.get("size"), "modified_at": item.get("modified_at"),
            "family": details.get("family"), "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
            "loaded": False, "context_length": None, "size_loaded": None, "size_vram": None, "gpu_percent": None, "expires_at": None,
        }
        state = loaded.get(name)
        if state is not None:
            size, vram = float(state.get("size") or 0), float(state.get("size_vram") or 0)
            model.update(loaded=True, context_length=state.get("context_length"), size_loaded=state.get("size"), size_vram=state.get("size_vram"), expires_at=state.get("expires_at"))
            if size > 0:
                model["gpu_percent"] = round(vram * 100 / size)
        models.append(model)
    models.sort(key=lambda model: model["name"])
    return {"online": True, "version": (version or {}).get("version") if isinstance(version, dict) else None, "models": models}


def docker_status(base: str) -> Dict[str, Any]:
    try:
        engines, status = get_json(base + "/engines/status")
    except httpx.HTTPError as error:
        return {"online": False, "error": str(error), "engines": None, "models": [], "disk": None}
    if status >= 400:
        return {"online": False, "error": "It answered, but not as Docker Model Runner.", "engines": None, "models": [], "disk": None}
    try:
        listed, _ = get_json(base + "/models", timeout=8)
        running, _ = get_json(base + "/engines/ps", timeout=4)
    except httpx.HTTPError as error:
        return {"online": True, "error": f"Model Runner couldn't list its models: {error}", "engines": engines, "models": [], "disk": None}
    loaded = {normalize_ref(str(item.get("model_name") or "")) for item in running or [] if isinstance(item, dict)}
    models = []
    for item in listed or []:
        if not isinstance(item, dict):
            continue
        tags = [str(tag) for tag in item.get("tags") or []]
        name = display_name(tags[0]) if tags else str(item.get("id") or "")
        config = item.get("config") or {}
        models.append({
            "name": name, "id": item.get("id"), "tags": [display_name(tag) for tag in tags], "created": item.get("created"),
            "format": config.get("format"), "quantization": config.get("quantization"), "parameters": config.get("parameters"), "size": config.get("size"),
            "loaded": any(normalize_ref(tag) in loaded for tag in tags),
        })
    models.sort(key=lambda model: -(model.get("created") or 0))
    return {"online": True, "engines": engines, "models": models, "disk": None}


REGISTRY_PREFIXES = ("index.docker.io/", "docker.io/", "registry-1.docker.io/")


def normalize_ref(ref: str) -> str:
    value = ref.strip().lower()
    for prefix in REGISTRY_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value[:-len(":latest")] if value.endswith(":latest") else value


def display_name(tag: str) -> str:
    for prefix in REGISTRY_PREFIXES:
        if tag.startswith(prefix):
            tag = tag[len(prefix):]
            break
    return tag[:-len(":latest")] if tag.endswith(":latest") else tag


def model_path(name: str) -> str:
    """A model reference as a URL path, keeping its slashes: ai/qwen3:8B-Q4_K_M."""
    return "/".join(quote(part, safe=":@") for part in name.strip().split("/"))


def tail(path, size: int) -> str:
    size = max(1024, min(size or 200_000, 2_000_000))
    with open(path, "rb") as file:
        file.seek(0, os.SEEK_END)
        file.seek(max(0, file.tell() - size))
        return file.read().decode("utf-8", "replace")


def detect() -> List[Dict[str, Any]]:
    """Which model servers answer on their default ports on loopback."""
    found: List[Dict[str, Any]] = []
    with httpx.Client(trust_env=False, timeout=1.5) as http:
        for kind, path, port in PROBES:
            try:
                response = http.get(f"http://127.0.0.1:{port}{path}")
            except httpx.HTTPError:
                continue
            # Strata answers 401 without its key; it's still there.
            if response.status_code != 200 and not (kind == "strata" and response.status_code == 401):
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            provider: Dict[str, Any] = {"type": kind, "port": port}
            if isinstance(body, dict):
                if isinstance(body.get("version"), str):
                    provider["version"] = body["version"]
                if isinstance(body.get("data"), list):
                    provider["models"] = len(body["data"])
            if kind == "ollama":
                try:
                    tags = http.get(f"http://127.0.0.1:{port}/api/tags").json()
                    provider["models"] = len(tags.get("models") or [])
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
            found.append(provider)
    return found


# This machine's load: CPU, memory and NVIDIA GPUs. Sampled when the server asks, never stored.

NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
_metrics_lock = threading.Lock()
_last: Dict[str, Any] = {"at": 0.0, "value": None, "cpu": None}


def metrics() -> Dict[str, Any]:
    """A fresh sample, shared between callers within a second."""
    with _metrics_lock:
        if _last["value"] is not None and time.monotonic() - _last["at"] < 1:
            return _last["value"]
        gpus, gpu_error = nvidia_gpus()
        used, total = memory()
        state: Dict[str, Any] = {"used": None, "total": None, "percent": None}
        if total:
            state["total"] = total
            if used:
                state.update(used=used, percent=math.floor(used * 1000 / total) / 10)
        value = {
            "hostname": socket.gethostname(), "platform": f"{platform.system()} {platform.release()}".strip(), "timestamp": time.time(),
            "cpu": {"percent": cpu_percent(), "count": os.cpu_count()},
            "memory": state, "gpus": gpus, "gpu_source": "nvidia-smi" if gpus else None, "gpu_error": gpu_error or None,
        }
        _last.update(at=time.monotonic(), value=value)
        return value


def cpu_percent() -> Optional[float]:
    try:
        import psutil

        return round(psutil.cpu_percent(interval=0.3), 1)
    except ImportError:
        pass
    times = proc_cpu_times()
    if times is None:
        return None
    time.sleep(0.3)
    later = proc_cpu_times()
    if later is None or later[1] <= times[1]:
        return None
    busy = 1 - (later[0] - times[0]) / (later[1] - times[1])
    return math.floor(max(0.0, min(busy, 1.0)) * 1000) / 10


def proc_cpu_times() -> Optional[Tuple[int, int]]:
    """Idle and total jiffies from /proc/stat's first line."""
    try:
        with open("/proc/stat") as file:
            fields = file.readline().split()
    except OSError:
        return None
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    values = [int(value) for value in fields[1:]]
    return values[3] + (values[4] if len(values) > 4 else 0), sum(values)


def memory() -> Tuple[int, int]:
    """Bytes in use and in all."""
    try:
        import psutil

        info = psutil.virtual_memory()
        return int(info.total - info.available), int(info.total)
    except ImportError:
        pass
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]

        status = Status()
        status.length = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total - status.available), int(status.total)
        return 0, 0
    values: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as file:
            for line in file:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[name] = int(parts[0]) * 1024
    except OSError:
        return 0, 0
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    if not available or available > total:
        return 0, total
    return total - available, total


def nvidia_gpus() -> Tuple[List[Dict[str, Any]], str]:
    program = shutil.which("nvidia-smi")
    if not program:
        return [], "nvidia-smi not found, so GPU usage is unavailable on this machine"
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    try:
        output = subprocess.run([program, "--query-gpu=" + NVIDIA_QUERY, "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, **flags)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"nvidia-smi failed: {error}"
    if output.returncode != 0:
        return [], f"nvidia-smi failed: exit status {output.returncode}"
    return parse_nvidia(output.stdout), ""


def parse_nvidia(output: str) -> List[Dict[str, Any]]:
    """nvidia-smi's CSV; memory comes in MiB."""
    def number(text: str) -> Optional[float]:
        try:
            return float(text)
        except ValueError:
            return None  # "[N/A]", for a field the GPU doesn't report

    def mib(text: str) -> Optional[int]:
        value = number(text)
        return int(value * 1024 * 1024) if value is not None else None

    gpus = []
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        gpus.append({
            "index": int(parts[0]), "name": parts[1], "utilization": number(parts[2]), "memory_used": mib(parts[3]),
            "memory_total": mib(parts[4]), "temperature": number(parts[5]), "power_watts": number(parts[6]) if len(parts) > 6 else None,
        })
    return gpus
