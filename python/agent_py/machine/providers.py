"""Answers the server's questions about the model servers on this machine ({"type": "provider"}
over the fleet connection): their state, their models (pull, delete, load, unload), their
logs, this machine's load, and the model catalogues to pull from (catalog.py). Also finds
the model servers running here, for the hello (detect, with_strata, with_keys).

The server never talks to a model server to manage it; it asks the machine that runs it.
Requests only name a kind, a loopback port and a model, and each op calls fixed paths on
127.0.0.1:<port>, so the server can't reach anything else through here. Reading is always
allowed; changing models needs `agent-machine servers on`.

The same as the Go agent-machine's machine/internal/providers and presence/providers.go,
op for op: Ollama, Docker Model Runner and Strata (its saved profiles and live instances read
from Strata's own folders for its key and logs, its load and unload).
"""

from __future__ import annotations

import concurrent.futures
import glob
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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from .. import common
from . import catalog, settings

Progress = Callable[[Dict[str, Any]], None]

KINDS = {"ollama": "Ollama", "strata": "Strata", "docker": "Docker Model Runner"}
CHANGES = {"pull", "delete", "load", "unload"}
# Where the Ollama agent-machine starts listens (servers.py).
OLLAMA_PORT = 11434

# Where each kind of model server listens by default, and the path that says it's there.
PROBES = (("ollama", "/api/version", 11434), ("docker", "/engines/v1/models", 12434), ("strata", "/v1/models", 18500))

MINUTE = 60.0


class Refused(Exception):
    pass


def local(timeout: Optional[float] = None) -> httpx.Client:
    """Model servers get a plain client, without the server's headers: they're not the server's business."""
    return httpx.Client(trust_env=False, timeout=timeout)


# JSON values read the way the Go agent-machine reads them.

def obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def items(value: Any) -> Optional[List[Any]]:
    return value if isinstance(value, list) else None


def text(value: Any) -> str:
    """A string, number or bool as text; anything else is empty."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        fixed = format(Decimal(repr(value)), "f")  # Go's 'f' with the shortest precision: 5 for 5.0, never 1e+21
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    return ""


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def whole(value: Any) -> Optional[int]:
    """A JSON number that is a whole number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not value.is_integer() or math.isinf(value)):
        return None
    return int(value)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (int, float)):
        return value != 0
    return value is not None


def error_text(data: bytes, status: int, who: str) -> str:
    """The readable error in a JSON error body, else the body, else the status."""
    try:
        body = json.loads(data)
    except ValueError:
        body = None
    found = obj(body)
    if found:
        value = found.get("error")
        if value is None:
            value = found.get("message")
        if isinstance(value, dict):
            value = value.get("message")
        if text(value):
            return text(value)
    stripped = data.decode("utf-8", "replace").strip()
    return stripped or f"{who} returned {status}"


def unavailable(who: str, error: Exception) -> Refused:
    return Refused(f"{who} unavailable: {error}")


def send(method: str, url: str, body: Any = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> httpx.Response:
    """One request, its answer read in full."""
    with local(timeout) as http:
        return http.request(method, url, json=body, headers=headers)


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 5) -> Tuple[Any, int]:
    """A GET's answer decoded (None when it isn't JSON) and its status; raises httpx.HTTPError when nothing answered."""
    response = send("GET", url, None, headers, timeout)
    try:
        return response.json(), response.status_code
    except ValueError:
        return None, response.status_code


def attempt(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 5) -> Tuple[Any, int, Optional[Exception]]:
    """get_json, with the failure returned rather than raised, for calls made together."""
    try:
        value, status = get_json(url, headers, timeout)
        return value, status, None
    except httpx.HTTPError as error:
        return None, 0, error


def together(*calls: Callable[[], Any]) -> List[Any]:
    """Runs the calls at the same time, as the Go agent-machine's goroutines do."""
    with concurrent.futures.ThreadPoolExecutor(len(calls)) as pool:
        return [future.result() for future in [pool.submit(call) for call in calls]]


def stream(url: str, body: Any, who: str, line: Callable[[Dict[str, Any]], None], headers: Optional[Dict[str, str]] = None, error_who: str = "") -> None:
    """Calls line for each JSON object line of a streamed answer."""
    with local() as http:
        try:
            response = http.send(http.build_request("POST", url, json=body, headers=headers), stream=True)
        except httpx.HTTPError as error:
            raise unavailable(who, error)
        try:
            if response.status_code >= 400:
                raise Refused(error_text(response.read()[:64 << 10], response.status_code, error_who or who))
            try:
                for raw in response.iter_lines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        line(value)
            except httpx.HTTPError as error:
                raise Refused(str(error))
        finally:
            response.close()


class Manager:
    """Answers provider requests. machine reads the machine's settings fresh for each request."""

    def __init__(self, machine: Callable[[], settings.Machine] = settings.Machine.load, host: str = "127.0.0.1") -> None:
        self.machine = machine
        self.host = host
        self.catalogs = catalog.Catalogs()

    def handle(self, request: Dict[str, Any], progress: Optional[Progress] = None) -> Dict[str, Any]:
        """One request, always answered with a provider_result for its id. progress hears a pull's progress before the answer."""
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
            try:
                return {"result": self.catalogs.search(str(request.get("catalog") or ""), str(request.get("query") or ""), str(request.get("repo") or ""))}
            except catalog.Failed as error:
                raise Refused(str(error))
        kind = str(request.get("kind") or "")
        if kind not in KINDS:
            raise Refused(f"unknown model server kind {json.dumps(kind)}")
        port = request.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
            raise Refused("name the model server's port")
        if op in CHANGES and not self.machine().servers:
            raise Refused("Managing model servers is off on this machine (agent-machine servers on)")
        base = f"http://{self.host}:{port}"
        model = str(request.get("model") or "").strip()
        if op in CHANGES and kind != "strata" and not model:
            raise Refused("A model name is required")
        key = str(request.get("key") or "")
        tail_size = whole(request.get("tail")) or 0
        which = f"{kind} {op}"
        if which == "ollama status":
            return {"state": ollama_status(base)}
        if which == "ollama pull":
            ollama_pull(base, model, progress)
            return {}
        if which == "ollama delete":
            ollama_call("DELETE", base + "/api/delete", {"model": model}, MINUTE)
            return result("deleted", model)
        if which == "ollama load":
            ollama_call("POST", base + "/api/generate", {"model": model, "stream": False}, 10 * MINUTE)
            return result("loaded", model)
        if which == "ollama unload":
            ollama_call("POST", base + "/api/generate", {"model": model, "keep_alive": 0, "stream": False}, MINUTE)
            return result("unloaded", model)
        if which == "ollama log":
            return {"text": ollama_log(port, tail_size)}
        if which == "strata status":
            return {"state": strata_status(base, port, key)}
        if which in ("strata load", "strata unload"):
            return {"result": {"status": strata_control(base, port, key, op)}}
        if which == "strata log":
            return {"text": strata_log(port, str(request.get("source") or ""), tail_size)}
        if which == "docker status":
            return {"state": docker_status(base)}
        if which == "docker pull":
            docker_pull(base, model, progress)
            return {}
        if which == "docker delete":
            docker_call("DELETE", base + "/models/" + model_path(model), None, None, MINUTE)
            return result("deleted", model)
        if which == "docker load":
            docker_call("POST", base + "/engines/v1/chat/completions", {"model": model, "messages": []}, {"X-Preload-Only": "true"}, 10 * MINUTE)
            return result("loaded", model)
        if which == "docker unload":
            return {"result": {"unloaded": model, "runners": docker_unload(base, model)}}
        raise Refused(f"{KINDS[kind]} can't {op}")


def result(key: str, model: str) -> Dict[str, Any]:
    return {"result": {key: model}}


def tail(path, size: int = 0) -> str:
    """The end of a file, at most size bytes (200 kB by default, 2 MB at most)."""
    if size <= 0:
        size = 200_000
    size = max(1024, min(size, 2_000_000))
    with open(path, "rb") as file:
        file.seek(0, os.SEEK_END)
        file.seek(max(0, file.tell() - size))
        return file.read().decode("utf-8", "replace")


# Ollama

def ollama_status(base: str) -> Dict[str, Any]:
    """The Ollama's models, from /api/tags, with what /api/ps says is loaded."""
    (tags, tags_status, tags_error), (running, _, _), (version, _, _) = together(
        lambda: attempt(base + "/api/tags"), lambda: attempt(base + "/api/ps"), lambda: attempt(base + "/api/version"))
    if tags_error is not None:
        return {"online": False, "error": str(tags_error), "models": []}
    if tags_status >= 400:
        return {"online": False, "error": f"Ollama returned {tags_status}", "models": []}
    loaded = {text(obj(item).get("name")): obj(item) for item in items(obj(running).get("models")) or []}
    models = []
    for item in items(obj(tags).get("models")) or []:
        entry = obj(item)
        details = obj(entry.get("details"))
        name = text(entry.get("name"))
        model: Dict[str, Any] = {
            "name": name, "size": entry.get("size"), "modified_at": entry.get("modified_at"),
            "family": details.get("family"), "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
            "loaded": False, "context_length": None, "size_loaded": None, "size_vram": None, "gpu_percent": None, "expires_at": None,
        }
        state = loaded.get(name)
        if state is not None:
            size, vram = number(state.get("size")), number(state.get("size_vram"))
            model.update(loaded=True, context_length=state.get("context_length"), size_loaded=state.get("size"), size_vram=state.get("size_vram"), expires_at=state.get("expires_at"))
            if size > 0:
                model["gpu_percent"] = math.floor(vram * 100 / size + 0.5)  # Go's math.Round: halves away from zero
        models.append(model)
    models.sort(key=lambda model: model["name"])
    return {"online": True, "version": obj(version).get("version"), "models": models}


def ollama_pull(base: str, model: str, progress: Progress) -> None:
    """Relays /api/pull's progress lines as they come."""
    failed: List[str] = []

    def line(update: Dict[str, Any]) -> None:
        if text(update.get("error")):
            failed.append(text(update["error"]))
            update["status"] = "error"
        progress(update)

    stream(base + "/api/pull", {"model": model, "stream": True}, "Ollama", line)
    if failed:
        raise Refused(failed[-1])


def ollama_call(method: str, url: str, body: Any, timeout: float) -> None:
    try:
        response = send(method, url, body, None, timeout)
    except httpx.HTTPError as error:
        raise unavailable("Ollama", error)
    if response.status_code >= 400:
        raise Refused(error_text(response.content[:1 << 20], response.status_code, "Ollama"))


def ollama_log(port: int, size: int) -> str:
    """The log of the Ollama agent-machine started (servers.py writes it)."""
    if port != OLLAMA_PORT:
        raise Refused("Only the Ollama agent-machine starts (on port 11434) has a log here")
    try:
        return tail(settings.home() / "ollama.log", size)
    except FileNotFoundError:
        return "No log yet: agent-machine writes one when it starts Ollama."


# Docker Model Runner, Docker's own model server. Routes used, from docker/model-runner (pkg/inference):
#
#   GET    /engines/status    inference engines and their state; identifies Model Runner
#   GET    /models            pulled models: id, tags and config (format, quantization, size, ...)
#   GET    /engines/ps        models loaded right now
#   GET    /engines/df        disk used by models
#   POST   /models/create     pull; JSON progress lines when sent Accept: application/json
#   DELETE /models/{ref}      delete a model
#   POST   /engines/unload    free a loaded model's memory
#   POST   /engines/v1/chat/completions with X-Preload-Only: true    load a model without generating

REGISTRY_PREFIXES = ("index.docker.io/", "docker.io/", "registry-1.docker.io/")


def normalize_ref(ref: str) -> str:
    """Makes 'docker.io/ai/qwen3:latest' and 'ai/qwen3' the same."""
    value = ref.strip().lower()
    for prefix in REGISTRY_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value[:-len(":latest")] if value.endswith(":latest") else value


def display_name(tag: str) -> str:
    """The name agents send: the tag without the registry or ':latest'."""
    for prefix in REGISTRY_PREFIXES:
        if tag.startswith(prefix):
            tag = tag[len(prefix):]
            break
    return tag[:-len(":latest")] if tag.endswith(":latest") else tag


def model_path(name: str) -> str:
    """A model reference as a URL path, keeping its slashes: ai/qwen3:8B-Q4_K_M."""
    return "/".join(quote(part, safe="$&+:=@") for part in name.strip().split("/"))  # what Go's url.PathEscape keeps


def tags_of(item: Dict[str, Any]) -> List[str]:
    return [text(tag) for tag in items(item.get("tags")) or [] if text(tag)]


def docker_models(listed: Any, running: Any) -> List[Dict[str, Any]]:
    """The pulled models, newest first, with whether each is loaded now."""
    loaded: Dict[str, Dict[str, Any]] = {}
    for item in items(running) or []:
        entry = obj(item)
        name = text(entry.get("model_name"))
        if name:
            loaded[normalize_ref(name)] = entry
    models = []
    for raw in items(listed) or []:
        item = obj(raw)
        model_id = text(item.get("id"))
        if not model_id:
            continue
        tags = tags_of(item)
        config = obj(item.get("config"))
        state = next((loaded[key] for key in [normalize_ref(model_id)] + [normalize_ref(tag) for tag in tags] if key in loaded), None)
        model: Dict[str, Any] = {
            "id": model_id, "name": display_name(tags[0]) if tags else model_id, "tags": [display_name(tag) for tag in tags], "created": item.get("created"),
            "format": config.get("format"), "quantization": config.get("quantization"), "parameters": config.get("parameters"),
            "architecture": config.get("architecture"), "size": config.get("size"), "context_size": config.get("context_size"),
            "loaded": False, "loading": False, "in_use": False, "engine": None,
        }
        if state is not None:
            loading = state.get("loading") is True
            model.update(loaded=not loading, loading=loading, in_use=state.get("in_use") is True, engine=state.get("backend_name"))
        models.append(model)
    models.sort(key=lambda model: -number(model["created"]))
    return models


def running_refs(listed: Any, running: Any, name: str) -> List[str]:
    """The names a model is loaded under now: a request may have loaded it by any of its tags."""
    keys = {normalize_ref(name)}
    for raw in items(listed) or []:
        item = obj(raw)
        names = [normalize_ref(text(item.get("id")))] + [normalize_ref(tag) for tag in tags_of(item)]
        if any(candidate in keys for candidate in names):
            keys.update(names)
    refs = []
    for raw in items(running) or []:
        model = text(obj(raw).get("model_name"))
        if model and normalize_ref(model) in keys:
            refs.append(model)
    return refs


def docker_status(base: str) -> Dict[str, Any]:
    def empty(online: bool, error: str, engines: Any = None) -> Dict[str, Any]:
        return {"online": online, "error": error, "engines": engines if engines is not None else {}, "models": [], "disk": None}

    try:
        engines, status = get_json(base + "/engines/status", timeout=4)
    except httpx.HTTPError as error:
        return empty(False, str(error))
    if status >= 400 or not isinstance(engines, dict) or not all(isinstance(value, str) for value in engines.values()):
        return empty(False, "It answered, but not as Docker Model Runner.")
    (listed, list_status, list_error), (running, run_status, run_error), (disk, disk_status, disk_error) = together(
        lambda: attempt(base + "/models", timeout=8), lambda: attempt(base + "/engines/ps", timeout=4), lambda: attempt(base + "/engines/df", timeout=4))
    if list_error is not None or list_status >= 400:
        detail = str(list_error) if list_error is not None else f"status {list_status}"
        return empty(True, "Model Runner couldn't list its models: " + detail, engines)
    if run_error is not None or run_status >= 400:
        running = None
    usage = disk if disk_error is None and disk_status < 400 and isinstance(disk, dict) else None
    return {"online": True, "engines": engines, "models": docker_models(listed, running), "disk": usage}


def pull_progress(message: Dict[str, Any], layers: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """One of Model Runner's pull messages as Ollama-style progress. layers collects the bytes
    done per layer, so completed covers the whole model."""
    kind = text(message.get("type"))
    if kind == "error":
        return {"status": "error", "error": text(message.get("message")) or "Pull failed"}
    if kind == "success":
        return {"status": "success"}
    if kind == "warning":
        return {"status": text(message.get("message")) or "warning"}
    if kind != "progress":
        return None
    layer = obj(message.get("layer"))
    layers[text(layer.get("id"))] = number(layer.get("current"))
    total = number(message.get("total"))
    completed = sum(layers.values())
    update: Dict[str, Any] = {"status": "downloading", "total": None, "completed": int(completed)}
    if total > 0:
        update["total"], update["completed"] = int(total), int(min(completed, total))
    return update


def docker_pull(base: str, model: str, progress: Progress) -> None:
    layers: Dict[str, float] = {}
    failed: List[str] = []

    def line(message: Dict[str, Any]) -> None:
        update = pull_progress(message, layers)
        if update is not None:
            if update["status"] == "error":
                failed.append(update["error"])
            progress(update)

    stream(base + "/models/create", {"from": model}, "Docker Model Runner", line, headers={"Accept": "application/json"}, error_who="Model Runner")
    if failed:
        raise Refused(failed[-1])


def docker_call(method: str, url: str, body: Any, headers: Optional[Dict[str, str]], timeout: float) -> Any:
    try:
        response = send(method, url, body, headers, timeout)
    except httpx.HTTPError as error:
        raise unavailable("Docker Model Runner", error)
    if response.status_code >= 400:
        raise Refused(error_text(response.content[:16 << 20], response.status_code, "Docker Model Runner"))
    try:
        return response.json()
    except ValueError:
        return None


def docker_unload(base: str, model: str) -> Any:
    """Frees a model under every name it's loaded by."""
    listed = docker_call("GET", base + "/models", None, None, 8)
    running = docker_call("GET", base + "/engines/ps", None, None, 4)
    refs = running_refs(listed, running, model) or [model]
    answer = docker_call("POST", base + "/engines/unload", {"models": refs}, None, MINUTE)
    return obj(answer).get("unloaded_runners")


# Strata serves one local model and asks for an API key on /v1. Its Desktop app and Terminal
# share these places (strata_control.profiles, .runtime and .paths in Strata's install):
#
#   profiles  %APPDATA%\Strata\profiles\<id>.json, else $XDG_CONFIG_HOME/strata/profiles.
#             "source_config" names the run config: host, port, model_name, api_key, log.
#   runtime   %LOCALAPPDATA%\Strata\runtime, else $XDG_STATE_HOME/strata/runtime.
#             <id>-<hex>.ready.json exists while an instance serves (api_url, model_id,
#             model_state, pid); <id>.server.log is that instance's request log.
#
# Only Strata's files are read here; starting and stopping Strata is servers.py's.

@dataclass
class Instance:
    """One saved Strata profile and, while it serves, its live instance."""

    profile: str
    url: str
    running: bool = False
    model: Any = None
    model_state: Any = None
    pid: Any = None
    config_path: str = ""
    engine_log: str = ""
    server_log: str = ""
    # Never sent to the console; the server gets it in the hello to forward traffic.
    api_key: str = ""

    def public(self) -> Dict[str, Any]:
        """The instance for the console."""
        logs = (["server"] if self.server_log else []) + (["engine"] if self.engine_log else [])
        return {
            "profile": self.profile, "url": self.url, "running": self.running, "model": self.model, "model_state": self.model_state,
            "pid": self.pid, "config_path": self.config_path or None, "has_key": bool(self.api_key), "logs": logs,
        }

    def port(self) -> int:
        """Where the instance serves."""
        try:
            return urlparse(self.url).port or 0
        except ValueError:
            return 0


def strata_profile_dir() -> str:
    if os.environ.get("STRATA_PROFILE_DIR"):
        return os.environ["STRATA_PROFILE_DIR"]
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), "Strata", "profiles")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "strata", "profiles")


def strata_runtime_dir() -> str:
    if os.environ.get("STRATA_RUNTIME_DIR"):
        return os.environ["STRATA_RUNTIME_DIR"]
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"), "Strata", "runtime")
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "strata", "runtime")


def first_key(value: Any) -> str:
    """The first key a Strata server accepts, in its forms: "k1,k2", a list, or "none" for no key."""
    if isinstance(value, list):
        keys = [text(item).strip() for item in value]
    elif isinstance(value, str):
        if value.strip().lower() == "none":
            return ""
        keys = [item.strip() for item in value.split(",")]
    else:
        keys = []
    return next((key for key in keys if key), "")


def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as file:
            return obj(json.loads(file.read()))
    except (OSError, ValueError):
        return {}


def is_file(path: str) -> bool:
    return os.path.isfile(path)


def discover() -> List[Instance]:
    """The saved Strata profiles on this machine, serving ones first."""
    paths = sorted(glob.glob(os.path.join(glob.escape(strata_profile_dir()), "*.json")))
    runtime_dir = strata_runtime_dir()
    found: List[Instance] = []
    for path in paths:
        if os.path.basename(path).startswith("."):
            continue
        profile = read_json(path)
        if not profile:
            continue
        name = text(profile.get("id")) or os.path.basename(path)[:-len(".json")]
        source = text(profile.get("source_config"))
        config = read_json(source) if source else {}
        config.update(obj(profile.get("overrides")))
        ready = ready_file(runtime_dir, name)
        address = text(ready.get("api_url")) or config_url(config)
        if not address:
            continue
        instance = Instance(profile=name, url=address.rstrip("/"), running=bool(ready), model=ready.get("model_id"), config_path=source, api_key=first_key(config.get("api_key")))
        if instance.model is None or instance.model == "":
            instance.model = config.get("model_name")
        if ready:
            instance.model_state, instance.pid = ready.get("model_state"), ready.get("pid")
        server_log = os.path.join(runtime_dir, name + ".server.log")
        if is_file(server_log):
            instance.server_log = server_log
        engine_log = text(config.get("log"))
        if engine_log and is_file(engine_log):
            instance.engine_log = engine_log
        found.append(instance)
    found.sort(key=lambda instance: not instance.running)
    return found


def ready_file(folder: str, profile: str) -> Dict[str, Any]:
    """The newest ready file for a profile whose process is still alive; a crash can leave a stale one behind."""
    newest: Dict[str, Any] = {}
    for path in glob.glob(os.path.join(glob.escape(folder), glob.escape(profile) + "-*.ready.json")):
        ready = read_json(path)
        pid = whole(ready.get("pid"))
        if pid is None or not settings.alive(pid):
            continue
        if not newest or number(ready.get("created_at")) > number(newest.get("created_at")):
            newest = ready
    return newest


def config_url(config: Dict[str, Any]) -> str:
    port = whole(config.get("port"))
    if port is None:
        return ""
    host = text(config.get("host"))
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}


def instance_on(port: int) -> Optional[Instance]:
    """The profile serving on this port on loopback, if any."""
    for instance in discover():
        try:
            host = urlparse(instance.url).hostname or ""
        except ValueError:
            continue
        if host in LOOPBACK_HOSTS and instance.port() == port:
            return instance
    return None


def strata_key(port: int, given: str) -> Tuple[str, Optional[str]]:
    """The key for the Strata on port and where it came from: the server's (for its containers),
    STRATA_API_KEY, or the profile serving there."""
    if given:
        return given, "container"
    key = first_key(os.environ.get("STRATA_API_KEY", ""))
    if key:
        return key, "environment"
    instance = instance_on(port)
    if instance is not None and instance.api_key:
        return instance.api_key, "profile " + instance.profile
    return "", None


def strata_status(base: str, port: int, given: str) -> Dict[str, Any]:
    key, source = strata_key(port, given)
    headers = {"Authorization": "Bearer " + key} if key else {}
    found = instance_on(port)
    instance = found.public() if found is not None else None
    unknown = {"state": "unknown", "source": source}
    try:
        health, _ = get_json(base + "/health", timeout=4)
    except httpx.HTTPError as error:
        return {"online": False, "error": str(error), "key": unknown, "models": [], "instance": instance}
    facts = obj(health)
    if facts.get("service") != "strata":
        return {"online": False, "error": "It answered, but not as a Strata server.", "key": unknown, "models": [], "instance": instance}
    (status, status_code, status_error), (models, models_code, models_error) = together(
        lambda: attempt(base + "/v1/status", headers, 4), lambda: attempt(base + "/v1/models", headers, 4))
    failure = status_error or models_error
    if failure is not None:
        return {"online": False, "error": str(failure), "health": facts, "key": unknown, "models": [], "instance": instance}
    state = "not_required"
    if status_code == 401:
        state = "rejected" if key else "missing"
    elif truthy(facts.get("api_key")):
        state = "ok"
    listed = items(obj(models).get("data")) if models_code < 400 else None
    if listed is None and text(facts.get("model")):
        # Without a key, /health still names the model.
        listed = [{"id": facts.get("model"), "status": {"value": "loaded" if truthy(facts.get("loaded")) else "unloaded"}, "meta": {"n_ctx": facts.get("max_context")}}]
    kept = [item for item in listed or [] if text(obj(item).get("id"))]
    return {
        "online": True, "health": facts, "status": obj(status) if status_code < 400 else None,
        "key": {"state": state, "source": source}, "models": kept, "instance": instance,
    }


def strata_control(base: str, port: int, given: str, action: str) -> str:
    """Loads or unloads the model. Strata accepts these from a JSON client without a browser Origin."""
    key, _ = strata_key(port, given)
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        response = send("POST", base + "/v1/" + action, {}, headers, 2 * MINUTE)
    except httpx.HTTPError as error:
        raise unavailable("Strata", error)
    data = response.content[:1 << 20]
    if response.status_code >= 400:
        raise Refused(error_text(data, response.status_code, "Strata"))
    try:
        body = json.loads(data)
    except ValueError:
        body = None
    return text(obj(body).get("status")) or action


def strata_log(port: int, source: str, size: int) -> str:
    """The end of the server or engine log of the profile serving on port, with its key blanked out."""
    source = source or "server"
    if source not in ("server", "engine"):
        raise Refused("Log source must be server or engine")
    instance = instance_on(port)
    if instance is None:
        raise Refused(f"No Strata profile on this machine serves on port {port}, so its logs can't be read.")
    path = instance.engine_log if source == "engine" else instance.server_log
    if not path:
        return ""
    try:
        found = tail(path, size)
    except OSError:
        return ""
    return found.replace(instance.api_key, "<api key>") if instance.api_key else found


# Model servers running here, for the hello (presence/providers.go).

def detect() -> List[Dict[str, Any]]:
    """Asks each default port on loopback whether its model server answers."""
    found: List[Dict[str, Any]] = []
    with httpx.Client(trust_env=False, timeout=1.5) as http:
        for kind, path, port in PROBES:
            try:
                response = http.get(f"http://127.0.0.1:{port}{path}")
            except httpx.HTTPError:
                continue
            try:
                body = obj(response.json())
            except ValueError:
                body = {}
            # Strata answers 401 without its key; it's still there.
            if response.status_code != 200 and not (kind == "strata" and response.status_code == 401):
                continue
            provider: Dict[str, Any] = {"type": kind, "port": port}
            if isinstance(body.get("version"), str) and body["version"]:
                provider["version"] = body["version"]
            if isinstance(body.get("data"), list):
                provider["models"] = len(body["data"])
            if kind == "ollama":
                provider.pop("models", None)
                count = ollama_models(http, port)
                if count is not None:
                    provider["models"] = count
            found.append(provider)
    return found


def ollama_models(http: httpx.Client, port: int) -> Optional[int]:
    try:
        tags = http.get(f"http://127.0.0.1:{port}/api/tags").json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(tags, dict):
        return None
    listed = tags.get("models")
    if listed is None:
        return 0
    return len(listed) if isinstance(listed, list) else None


def with_strata(found: List[Dict[str, Any]], serving: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Adds the Strata profiles serving on another port than Strata's default, as the servers'
    state reports them (a profile's own config picks its port)."""
    for item in items(obj(obj(serving).get("strata")).get("profiles")) or []:
        address = obj(item).get("url")
        if not isinstance(address, str) or not common.is_loopback_url(address):
            continue
        try:
            port = urlparse(address).port or 0
        except ValueError:
            continue
        if port > 0 and not any(provider.get("port") == port for provider in found):
            found.append({"type": "strata", "port": port})
    return found


def with_keys(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Adds the key of the Strata profile serving on each Strata provider's port. The server keeps
    it in memory only; it's never logged here."""
    instances: Optional[List[Instance]] = None
    for provider in found:
        if provider.get("type") != "strata":
            continue
        if instances is None:
            instances = discover()
        for instance in instances:
            if instance.port() == provider.get("port") and instance.api_key and common.is_loopback_url(instance.url):
                provider["api_key"] = instance.api_key
                break
    return found


# This machine's load: CPU, memory and NVIDIA GPUs. Sampled when the server asks, never stored.

NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
_metrics_lock = threading.Lock()
_last: Dict[str, Any] = {"at": 0.0, "value": None, "cpu_at": 0.0, "idle": 0, "all": 0, "cpu": None}


def metrics() -> Dict[str, Any]:
    """A fresh sample, shared between callers within a second."""
    with _metrics_lock:
        if _last["value"] is not None and time.monotonic() - _last["at"] < 1:
            return _last["value"]
        gpus, gpu_error = nvidia_gpus()
        used, total = memory()
        state: Dict[str, Any] = {"used": None, "total": None, "percent": None}
        if total > 0:
            state["total"] = total
            if used > 0:
                state.update(used=used, percent=math.floor(used * 1000 / total) / 10)
        value = {
            "hostname": socket.gethostname(), "platform": platform_name(), "timestamp": time.time(),
            "cpu": {"percent": cpu_percent(), "count": cpu_count()},
            "memory": state, "gpus": gpus, "gpu_source": "nvidia-smi" if gpus else None, "gpu_error": gpu_error or None,
        }
        _last.update(at=time.monotonic(), value=value)
        return value


def linux() -> bool:
    return sys.platform.startswith("linux") or sys.platform == "android"


def platform_name() -> str:
    if linux():
        try:
            with open("/proc/sys/kernel/osrelease") as file:
                release = file.read().strip()
        except OSError:
            release = ""
        return ("Linux " + release).strip()
    if sys.platform == "win32":
        return "Windows " + platform.version()
    return common.goos()


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0)) or 1  # what Go's runtime.NumCPU counts
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def cpu_percent() -> Optional[float]:
    """The CPU use since the last sample: the first needs a short wait, and samples very close
    together read near zero, so those reuse the last value."""
    state = _last
    if state["cpu_at"] and time.monotonic() - state["cpu_at"] < 0.5:
        return state["cpu"]
    times = cpu_times()
    if times is None:
        return psutil_cpu()
    idle, total = times
    if not state["cpu_at"]:
        state["idle"], state["all"] = idle, total
        time.sleep(0.3)
        times = cpu_times()
        if times is None:
            return None
        idle, total = times
    if total > state["all"]:
        busy = 1 - (idle - state["idle"]) / (total - state["all"])
        state["cpu"] = math.floor(max(0.0, min(busy, 1.0)) * 1000) / 10
    state["cpu_at"], state["idle"], state["all"] = time.monotonic(), idle, total
    return state["cpu"]


def psutil_cpu() -> Optional[float]:
    """Where the Go agent-machine can't read the CPU's use (macOS, BSD, Android without /proc/stat), psutil can, if installed."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return math.floor(psutil.cpu_percent(interval=0.3) * 10) / 10
    except Exception:
        return None


def cpu_times() -> Optional[Tuple[int, int]]:
    """Idle and total CPU time: /proc/stat's first line on Linux, GetSystemTimes on Windows."""
    if sys.platform == "win32":
        import ctypes

        class Filetime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        idle, kernel, user = Filetime(), Filetime(), Filetime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def value(filetime: Filetime) -> int:
            return filetime.high << 32 | filetime.low

        # Kernel time includes idle.
        return value(idle), value(kernel) + value(user)
    if not linux():
        return None
    try:
        with open("/proc/stat") as file:
            fields = file.readline().split()
    except OSError:
        return None
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    idle = total = 0
    for index, field in enumerate(fields[1:]):
        try:
            amount = int(field)
        except ValueError:
            amount = 0
        total += amount
        if index in (3, 4):  # idle and iowait
            idle += amount
    return idle, total


def memory() -> Tuple[int, int]:
    """Bytes in use and in all."""
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]

        status = Status()
        status.length = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) or not status.total:
            return 0, 0
        return int(status.total - status.available), int(status.total)
    if not linux():
        # The installed memory only, as the Go agent-machine reads it.
        total = sysctl("hw.memsize" if sys.platform == "darwin" else "hw.physmem")
        return 0, total
    values: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as file:
            for line in file:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        values[name] = int(parts[0]) * 1024
                    except ValueError:
                        values[name] = 0
    except OSError:
        return 0, 0
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    if not available or available > total:
        return 0, total
    return total - available, total


def sysctl(name: str) -> int:
    """One number with sysctl -n, for the systems without /proc."""
    try:
        output = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
        return int(output.stdout.strip()) if output.returncode == 0 else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


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
    def maybe_number(value: str) -> Optional[float]:
        try:
            return float(value)
        except ValueError:
            return None  # "[N/A]", for a field the GPU doesn't report

    def mib(value: str) -> Optional[int]:
        found = maybe_number(value)
        return int(found * 1024 * 1024) if found is not None else None

    gpus = []
    for line in output.strip().split("\n"):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        gpus.append({
            "index": index, "name": parts[1], "utilization": maybe_number(parts[2]), "memory_used": mib(parts[3]),
            "memory_total": mib(parts[4]), "temperature": maybe_number(parts[5]), "power_watts": maybe_number(parts[6]) if len(parts) > 6 else None,
        })
    return gpus
