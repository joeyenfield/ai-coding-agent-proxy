"""Answers the server's questions about the model servers on this machine ({"type": "provider"}
over the fleet connection): their state, their models (pull, delete, load, unload), their
logs, this machine's load, and the model catalogues to pull from (catalog.py). Also finds
the model servers running here, for the hello (detect, with_strata, with_keys).

The server never talks to a model server to manage it; it asks the machine that runs it.
Requests only name a kind, a loopback port and a model, and each op calls fixed paths on
127.0.0.1:<port>, so the server can't reach anything else through here. Reading is always
allowed; changing models needs `agent-machine servers on`.

The same as the Go agent-machine's machine/internal/providers and presence/providers.go,
op for op: Ollama, llama.cpp (llama-server's router), Docker Model Runner, Strata (its saved
profiles and live instances read from Strata's own folders for its key and logs, its load and unload),
and kind "api", other OpenAI-compatible servers (LM Studio, vLLM, SGLang), whose model list is only read.
"""

from __future__ import annotations

import concurrent.futures
import glob
import hashlib
import json
import math
import os
import platform
import re
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

KINDS = {"ollama": "Ollama", "strata": "Strata", "docker": "Docker Model Runner", "llamacpp": "llama.cpp", "api": "An OpenAI-compatible server"}
CHANGES = {"pull", "delete", "load", "unload"}
# Where the Ollama agent-machine starts listens (servers.py).
OLLAMA_PORT = 11434
# Where llama-server listens by default (and where agent-machine starts it); AI_PROXY_LLAMACPP_PORT moves it.
LLAMA_PORT = 9931
LLAMA_PORT_ENV = "AI_PROXY_LLAMACPP_PORT"


def llama_server_port() -> int:
    """The port agent-machine's llama-server uses: AI_PROXY_LLAMACPP_PORT, else 9931."""
    try:
        port = int(os.environ.get(LLAMA_PORT_ENV, ""))
    except ValueError:
        return LLAMA_PORT
    return port if 0 < port < 65536 else LLAMA_PORT


# Where each kind of model server listens by default, and the path that says it's there.
# llama-server's router lists every model it can serve here, loaded or not (its port: llama_server_port()).
# What each kind of model server answers on, as detect() asks it.
KIND_PATHS = {"ollama": "/api/version", "docker": "/engines/v1/models", "strata": "/v1/models", "llamacpp": "/models"}
PROBES = (("ollama", "/api/version", 11434), ("docker", "/engines/v1/models", 12434), ("strata", "/v1/models", 18500), ("llamacpp", "/models", 0))
# Where the OpenAI-compatible servers that only serve (their models are managed in the server
# itself) listen by default. They're reported as type "api"; only their /v1/models is read.
API_PROBES = (("lmstudio", 1234), ("vllm", 8000), ("sglang", 30000))
# More ports with an OpenAI-compatible server, comma separated (flavor "openai").
API_PORTS_ENV = "AI_PROXY_API_PORTS"
API_MANAGED = "An OpenAI-compatible server's models are managed in that server itself (LM Studio, vLLM, SGLang, ...)"

MINUTE = 60.0


class Refused(Exception):
    pass


class NoDownloads(Refused):
    """A router too old to download, where agent-machine can download the files instead."""


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
        # Whether a port is one of this machine's model servers: those it reported, and its containers. The
        # fleet connection sets it; None (tests, the commands) trusts every port.
        self.serves: Optional[Callable[[int], bool]] = None
        self.catalogs = catalog.Catalogs()
        # Where llama.cpp model files are downloaded from, and the client that does it (no time limit); tests replace them.
        self.hugging_face_files_url = HUGGING_FACE_FILES_URL
        self.downloads: Callable[[], httpx.Client] = lambda: httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=300), follow_redirects=True)
        # Restarts the llama-server agent-machine runs, so it serves its GGUF folder; presence wires it to servers.
        self.restart_llama: Optional[Callable[[], None]] = None

    def answers_as(self, kind: str, port: int) -> bool:
        """Whether what listens on the port answers as that kind of model server. A change (pull, delete,
        load, unload) is only sent to one that does: the server names the port, and something else on
        loopback there must never get a POST or DELETE meant for a model server."""
        path = KIND_PATHS.get(kind)
        if path is None:
            return False
        try:
            with local(3) as http:
                response = http.get(f"http://{self.host}:{port}{path}")
        except httpx.HTTPError:
            return False
        # Strata answers 401 without its key; it's still Strata.
        if response.status_code != 200:
            return kind == "strata" and response.status_code == 401
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        return isinstance(body.get("version"), str) if kind == "ollama" else True

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
        if kind == "api":
            if op == "status":
                return {"state": api_status(base)}
            raise Refused(API_MANAGED)
        # The server names the port. One this machine found itself is its model server; any other has to
        # answer as that kind first (one just started, or one the machine doesn't look for).
        if op in CHANGES and self.serves is not None and not self.serves(port) and not self.answers_as(kind, port):
            raise Refused(f"nothing on port {port} answers as {kind}, so its models can't be changed")
        model = str(request.get("model") or "").strip()
        if (op in CHANGES or op == "files") and kind != "strata" and not model:
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
        if which == "llamacpp status":
            return {"state": llama_status(base, key)}
        if which == "llamacpp pull":
            self.llama_pull_by(str(request.get("method") or ""), base, key, model, port, progress)
            return {}
        if which == "llamacpp files":
            # Read-only: what downloading it by hand means (the files, their URLs, the folder).
            plan = llama_plan(self.catalogs, self.hugging_face_files_url, model)
            plan["serves_folder"] = llama_own(port)
            return {"result": plan}
        if which == "llamacpp reload":
            # After files were put in the GGUF folder by hand: the router reads it again.
            value, status, error = attempt(base + "/models?reload=1", llama_headers(key), 30)
            if error is not None:
                raise unavailable("llama-server", error)
            if status >= 400:
                raise Refused(f"llama-server returned {status}")
            return result("reloaded", llama_folder())
        if which == "llamacpp delete":
            if llama_remove_local(base, key, model):
                return result("deleted", model)
            llama_call("DELETE", base + "/models?model=" + quote(model, safe=""), key, None, MINUTE)
            return result("deleted", model)
        if which == "llamacpp load":
            # The router answers once the model is loading; its status says when it's in.
            llama_call("POST", base + "/models/load", key, {"model": model}, 10 * MINUTE)
            return result("loaded", model)
        if which == "llamacpp unload":
            llama_call("POST", base + "/models/unload", key, {"model": model}, MINUTE)
            return result("unloaded", model)
        if which == "llamacpp log":
            return {"text": llama_log(port, tail_size)}
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

    def llama_pull_by(self, method: str, base: str, key: str, model: str, port: int, progress: Progress) -> None:
        """Pulls with the router, or has agent-machine download the files (method "direct"), which it
        also does by itself when its own router is too old to."""
        if method == "direct":
            self.llama_fetch(base, key, model, port, progress)
            return
        if method not in ("", "router"):
            raise Refused(f"pull with the router or directly, not {json.dumps(method)}")
        try:
            llama_pull(base, key, model, progress)
        except NoDownloads:
            if method or not llama_own(port):
                raise
            progress({"status": "this llama-server can't download models: agent-machine downloads the files itself"})
            self.llama_fetch(base, key, model, port, progress)

    def llama_fetch(self, base: str, key: str, model: str, port: int, progress: Progress) -> None:
        """Downloads a model's files into its subfolder of the GGUF folder, each checked by its SHA-256,
        then has the router read the folder again."""
        if not llama_own(port):
            raise Refused(f"only the llama-server agent-machine starts (port {llama_server_port()}) serves its GGUF folder: "
                          "download with the router itself, or into this one's own folder by hand")
        plan = llama_plan(self.catalogs, self.hugging_face_files_url, model)
        os.makedirs(plan["dir"], exist_ok=True)
        total = sum(file["size"] for file in plan["files"])
        done = 0
        progress({"status": f"downloading {plan['model']} into {plan['dir']}", "completed": 0, "total": total})
        for file in plan["files"]:
            target = os.path.join(plan["folder"], file["path"])
            if os.path.isfile(target) and (not file["size"] or os.path.getsize(target) == file["size"]):
                done += os.path.getsize(target)
                continue
            start = done
            done += llama_download(self.downloads, file, target + ".part",
                                   lambda n: progress({"status": "downloading", "completed": start + n, "total": total}))
        # Only now take .part off, so the router never lists a model whose files aren't all there.
        for file in plan["files"]:
            target = os.path.join(plan["folder"], file["path"])
            if os.path.exists(target + ".part"):
                os.replace(target + ".part", target)
        llama_serve(base, key, plan["name"], self.restart_llama, progress)


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


# llama.cpp's llama-server in router mode (tools/server/README.md, "Using multiple models"). Routes used:
#
#   GET    /models               every model it can serve: the cache, its models folder; each with its status
#                                (unloaded, loading, loaded, sleeping, downloading, incomplete)
#   POST   /models               download a Hugging Face model ("user/repo:quant") into the cache, in the background
#   GET    /models/sse           the download's progress (download_progress, then download_finished or download_failed)
#   DELETE /models?model=        delete a cached model
#   POST   /models/load|unload   load or unload one
#
# A llama-server run with one model (-m) has no router: it lists that model, loaded, and can't change models.

def llama_model_name(model: str) -> None:
    """What can be downloaded: a Hugging Face repo, and optionally its quant."""
    repo, _, quant = model.partition(":")
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1] or any(char in model for char in " \t\r\n?#&") or len(model) > 300:
        raise Refused("name a Hugging Face model as user/repo or user/repo:quant (Q4_K_M, ...)")
    if "/" in quant:
        raise Refused("a quant is a name such as Q4_K_M")


def file_size(path: str) -> Optional[int]:
    if not path:
        return None
    try:
        if not os.path.isdir(path):
            return os.stat(path).st_size
    except OSError:
        return None
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            if name.lower().endswith(".gguf"):
                try:
                    total += os.stat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
    return total


def llama_models(listed: Any) -> List[Dict[str, Any]]:
    """The router's models as the console shows them, loaded first, then by name."""
    models = []
    for raw in items(obj(listed).get("data")) or []:
        item = obj(raw)
        model_id = text(item.get("id"))
        if not model_id:
            continue
        status = obj(item.get("status"))
        # One model, no router: it's what's loaded.
        state = text(status.get("value")) or "loaded"
        meta = obj(item.get("meta"))
        path = text(item.get("path"))
        model: Dict[str, Any] = {
            "name": model_id, "state": state, "loaded": state in ("loaded", "sleeping"),
            "failed": status.get("failed") is True, "path": path, "size": file_size(path),
            "context_train": meta.get("n_ctx_train"), "parameters": meta.get("n_params"), "progress": None,
            "input": obj(item.get("architecture")).get("input_modalities"),
        }
        if model["size"] is None and meta.get("size") is not None:
            model["size"] = meta["size"]
        progress = obj(status.get("progress"))
        if progress:
            done = sum(number(obj(file).get("done")) for file in progress.values())
            total = sum(number(obj(file).get("total")) for file in progress.values())
            model["progress"] = {"completed": int(done), "total": int(total)}
        models.append(model)
    models.sort(key=lambda model: (not model["loaded"], model["name"]))
    return models


def llama_headers(key: str) -> Optional[Dict[str, str]]:
    """The key a llama.cpp container takes (the server's, sent with the request), if any."""
    return {"Authorization": f"Bearer {key}"} if key else None


def llama_status(base: str, key: str) -> Dict[str, Any]:
    try:
        listed, status = get_json(base + "/models", llama_headers(key), 8)
    except httpx.HTTPError as error:
        return {"online": False, "error": str(error), "models": [], "router": False}
    if status >= 400:
        return {"online": False, "error": f"llama-server returned {status}", "models": [], "router": False}
    data = items(obj(listed).get("data")) or []
    # The router gives every model a status; a llama-server with one model doesn't.
    router = not data or any("status" in obj(raw) for raw in data)
    return {"online": True, "router": router, "models": llama_models(listed)}


def llama_call(method: str, url: str, key: str, body: Any, timeout: float) -> None:
    try:
        response = send(method, url, body, llama_headers(key), timeout)
    except httpx.HTTPError as error:
        raise unavailable("llama-server", error)
    data = response.content[:1 << 20]
    if response.status_code == 401:
        raise Refused("llama-server wants an API key this machine doesn't have")
    if response.status_code == 404:
        raise Refused("this llama-server serves one model: start it in router mode (without -m) to change its models")
    if response.status_code >= 400:
        raise Refused(error_text(data, response.status_code, "llama-server"))
    try:
        answer = json.loads(data)
    except ValueError:
        answer = None
    if obj(answer).get("success") is False:
        raise Refused(error_text(data, response.status_code, "llama-server"))


def same_model(event: str, model: str) -> bool:
    """Whether an event is about the model being pulled: the router may name it with or without its quant, and in any case."""
    event, model = event.lower(), model.lower()
    if event == model:
        return True
    repo = model.partition(":")[0]
    return ":" not in model and event.startswith(repo + ":")


def llama_too_old() -> str:
    """Why a router has no downloads: llama.cpp's router could only load and unload models before its June 2026
    builds (b9590 and older), which Termux mirrors that stopped syncing still hand out."""
    message = "this llama-server is too old to download models (llama.cpp added downloads to its router in mid-2026): update llama.cpp"
    if "com.termux" in os.environ.get("PREFIX", "") or os.environ.get("TERMUX_VERSION"):
        return message + ". In Termux: pkg upgrade llama-cpp; if it stays old, pick another mirror with termux-change-repo and upgrade again"
    return message + ", from the machine's llama.cpp server tab or however it was installed"


def llama_no_downloads(base: str, key: str, status: int) -> Refused:
    """Why the router's download progress stream was refused."""
    if status == 401:
        return Refused("llama-server wants an API key this machine doesn't have")
    if status == 404:
        # A router lists every model with a status; only an old one lacks the download routes.
        if llama_status(base, key).get("router") is True:
            return NoDownloads(llama_too_old())
        return Refused("this llama-server serves one model: start it in router mode (without -m) to download models")
    return Refused(f"llama-server answered {status} to /models/sse, so it can't report a download's progress")


# Ends a pull whose progress stream has gone quiet, as the Go machine does.
PULL_IDLE = 10 * 60


def llama_pull(base: str, key: str, model: str, progress: Progress) -> None:
    """Starts the router's download and relays its progress, Ollama's style, until it finishes."""
    llama_model_name(model)
    headers = {"Accept": "text/event-stream", **(llama_headers(key) or {})}
    with local() as http:
        # Listen first, so no event is missed between starting the download and listening.
        try:
            # A read timeout bounds a router that stops reporting (or never reports this
            # model), so the pull doesn't hold its worker and connection forever.
            request = http.build_request("GET", base + "/models/sse", headers=headers,
                                         timeout=httpx.Timeout(PULL_IDLE, connect=10))
            events = http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise unavailable("llama-server", error)
        try:
            if events.status_code >= 400:
                raise llama_no_downloads(base, key, events.status_code)
            llama_call("POST", base + "/models", key, {"model": model}, MINUTE)
            progress({"status": "pulling " + model})
            # Only this model's events count as progress: another model's download (or
            # heartbeats) keeping the stream busy must not keep a stalled pull alive.
            quiet = time.monotonic()
            try:
                for raw in events.iter_lines():
                    if time.monotonic() - quiet > PULL_IDLE:
                        raise Refused("llama-server reported no download progress for 10 minutes; giving up")
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or not same_model(text(event.get("model")), model):
                        continue
                    quiet = time.monotonic()
                    kind = text(event.get("event"))
                    if kind == "download_progress":
                        files = obj(event.get("data")).values()
                        done = sum(number(obj(file).get("done")) for file in files)
                        total = sum(number(obj(file).get("total")) for file in obj(event.get("data")).values())
                        progress({"status": "downloading", "completed": int(done), "total": int(total)})
                    elif kind == "download_finished":
                        # The router lists it once its models are read again.
                        attempt(base + "/models?reload=1", llama_headers(key), 30)
                        progress({"status": "success"})
                        return
                    elif kind == "download_failed":
                        data = obj(event.get("data"))
                        message = text(data.get("error")) or text(data.get("message")) or "the download failed"
                        progress({"status": "error", "error": message})
                        raise Refused(message)
            except httpx.ReadTimeout:
                raise Refused("llama-server reported no download progress for 10 minutes; giving up")
            except httpx.HTTPError as error:
                raise unavailable("llama-server", error)
            raise Refused("llama-server stopped before the download finished")
        finally:
            events.close()


# llama.cpp: downloading a model's GGUF files here, instead of through the router (llamacpp_files.go):
# for routers too old to download (llama.cpp before mid-2026, which stale Termux mirrors still hand
# out), or by choice. The files go into the GGUF folder agent-machine's llama-server serves
# (LLAMA_ARG_MODELS_DIR), one subfolder per model, which every router reads again on
# /models?reload=1. "files" gives the same plan for downloading them by hand (curl).

# Where Hugging Face serves a repo's files (<repo>/resolve/main/<path>).
HUGGING_FACE_FILES_URL = "https://huggingface.co"
# What "user/repo" alone downloads, as llama-server's -hf does.
DEFAULT_QUANT = "Q4_K_M"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def llama_folder() -> str:
    """The GGUF folder agent-machine's llama-server serves (llamacpp.py owns its setting)."""
    from . import llamacpp
    return llamacpp.models_dir()


def hf_token() -> str:
    """A Hugging Face token for gated repos, as llama.cpp itself reads it."""
    return os.environ.get("HF_TOKEN", "").strip()


def hf_headers() -> Dict[str, str]:
    headers = {"User-Agent": catalog.SEARCH_AGENT}
    if hf_token():
        headers["Authorization"] = "Bearer " + hf_token()
    return headers


def llama_own(port: int) -> bool:
    """Whether a port is the llama-server agent-machine starts, the one serving its GGUF folder."""
    return port == llama_server_port()


def pick_projector(projectors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The projector llama.cpp would take: F16 first, then BF16, then any."""
    for want in ("f16", "bf16", ""):
        for item in projectors:
            name = text(item.get("path")).lower()
            if not want or (want in name and not (want == "f16" and "bf16" in name)):
                return item
    return None


def llama_plan(catalogs: "catalog.Catalogs", files_url: str, model: str) -> Dict[str, Any]:
    """A model's files on Hugging Face: the quant's GGUF (every shard of a split one) and, as
    llama-server's -hf does, the repo's vision projector; and where they go."""
    llama_model_name(model)
    repo, _, quant = model.partition(":")
    try:
        with catalogs.client() as http:
            response = http.get(f"{catalogs.hugging_face_url}/{repo}/tree/main", params={"recursive": "1"}, headers=hf_headers())
    except httpx.HTTPError as error:
        raise Refused(f"couldn't reach Hugging Face: {error}")
    if response.status_code in (401, 403):
        raise Refused("this repo is gated or private: accept its licence on huggingface.co, and give agent-machine a token (HF_TOKEN)")
    if response.status_code == 404:
        raise Refused(f"{repo} isn't on Hugging Face")
    if response.status_code >= 400:
        raise Refused(f"Hugging Face returned {response.status_code}")
    try:
        listed = response.json()
    except ValueError:
        raise Refused("unexpected answer from Hugging Face")
    by_quant: Dict[str, List[Dict[str, Any]]] = {}
    projectors: List[Dict[str, Any]] = []
    for raw in items(listed) or []:
        item = obj(raw)
        path = text(item.get("path"))
        name = path.rsplit("/", 1)[-1]
        if text(item.get("type")) != "file" or not name.lower().endswith(".gguf"):
            continue
        if "mmproj" in name.lower():
            projectors.append(item)
            continue
        found = catalog.quant_from_filename(name[:-5])
        if found:
            by_quant.setdefault(found, []).append(item)
    available = sorted(by_quant)
    quant = quant.strip().upper()
    if not quant:
        quant = DEFAULT_QUANT
        if quant not in by_quant and len(available) == 1:
            quant = available[0]
    chosen = sorted(by_quant.get(quant, []), key=lambda item: text(item.get("path")))
    if not chosen:
        if not available:
            raise Refused(f"{repo} has no GGUF files: look for a repo ending in -GGUF")
        raise Refused(f"{repo} has no {quant}: it has {', '.join(available)}")
    projector = pick_projector(projectors)
    if projector is not None:
        chosen.append(projector)
    short = repo.split("/", 1)[1]
    if short.lower().endswith("-gguf"):
        short = short[:-5]
    name = _UNSAFE_NAME.sub("-", f"{short}-{quant}").strip("-.")
    folder = llama_folder()
    files = []
    for item in chosen:
        path = text(item.get("path"))
        lfs = obj(item.get("lfs"))
        size = int(number(lfs.get("size")) or number(item.get("size")))
        # The name is joined into the model's folder: on Windows a backslash or a drive would step out of it.
        local_name = path.rsplit("/", 1)[-1]
        if local_name in ("", ".", "..") or "\\" in local_name or ":" in local_name:
            raise Refused(f"{repo} has a file whose name can't be saved here: {path}")
        files.append({"repo_path": path, "path": os.path.join(name, local_name), "size": size, "sha256": text(lfs.get("oid")).lower(),
                      "url": f"{files_url}/{repo}/resolve/main/" + "/".join(quote(segment, safe="") for segment in path.split("/"))})
    return {"model": f"{repo}:{quant}", "name": name, "folder": folder, "dir": os.path.join(folder, name), "files": files}


def llama_download(client: Callable[[], httpx.Client], file: Dict[str, Any], part: str, report: Callable[[int], None]) -> int:
    """Writes one file to part, resuming what an earlier try left, and checks its SHA-256. Returns its size."""
    digest = hashlib.sha256()
    have = 0
    try:
        with open(part, "rb") as existing:
            for chunk in iter(lambda: existing.read(1 << 20), b""):
                digest.update(chunk)
                have += len(chunk)
    except FileNotFoundError:
        pass
    if file["size"] and have > file["size"]:
        have, digest = 0, hashlib.sha256()
    headers = hf_headers()
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        with client() as http, http.stream("GET", file["url"], headers=headers) as response:
            status = response.status_code
            if status == 206 and have:
                mode = "ab"
            elif status == 200:
                # Started again from the beginning (no range, or the server ignored it).
                have, digest, mode = 0, hashlib.sha256(), "wb"
            elif status == 416 and file["size"] and have == file["size"]:
                check_digest(file, digest)
                return have
            elif status in (401, 403):
                raise Refused("Hugging Face refused the download: the repo is gated or private (accept its licence, and give agent-machine a token: HF_TOKEN)")
            else:
                raise Refused(f"Hugging Face answered {status} for {file['repo_path']}")
            written, last = have, 0.0
            with open(part, mode) as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if time.monotonic() - last >= 1:
                        report(written)
                        last = time.monotonic()
            report(written)
    except httpx.HTTPError as error:
        raise Refused(f"the download of {file['repo_path']} stopped: {error} (pull it again to carry on)")
    if file["size"] and written != file["size"]:
        raise Refused(f"the download of {file['repo_path']} stopped at {written} of {file['size']} bytes (pull it again to carry on)")
    try:
        check_digest(file, digest)
    except Refused:
        os.remove(part)
        raise
    return written


def check_digest(file: Dict[str, Any], digest: Any) -> None:
    if file["sha256"] and digest.hexdigest() != file["sha256"]:
        raise Refused(f"{file['repo_path']} isn't what Hugging Face lists (SHA-256 {digest.hexdigest()}, not {file['sha256']}): deleted it, pull it again")


def llama_listed(base: str, key: str, name: str) -> bool:
    value, status, error = attempt(base + "/models?reload=1", llama_headers(key), 30)
    if error or status >= 400:
        return False
    return any(text(obj(raw).get("id")).lower() == name.lower() for raw in items(obj(value).get("data")) or [])


def llama_serve(base: str, key: str, name: str, restart: Optional[Callable[[], None]], progress: Progress) -> None:
    """Has the router read its GGUF folder again and checks it lists name; a router started before
    it served that folder has to restart for it."""
    if llama_listed(base, key, name):
        progress({"status": "success"})
        return
    if restart is None:
        raise Refused(f"downloaded into {llama_folder()}, but llama-server doesn't list it: restart llama.cpp from the machine's llama.cpp server tab")
    progress({"status": "restarting llama.cpp to serve " + llama_folder()})
    try:
        restart()
    except Refused as error:
        raise Refused(f"downloaded into {llama_folder()}, but couldn't restart llama.cpp to serve it: {error}")
    if not llama_listed(base, key, name):
        raise Refused(f"downloaded into {llama_folder()}, but llama-server doesn't list {name}: check its GGUF folder setting")
    progress({"status": "success"})


def llama_remove_local(base: str, key: str, name: str) -> bool:
    """Deletes a model agent-machine downloaded into its GGUF folder (the router deletes only what's
    in its cache); whether name was one."""
    if not name or name != os.path.basename(name) or name.startswith("."):
        return False
    folder = llama_folder()
    for path in (os.path.join(folder, name), os.path.join(folder, name + ".gguf")):
        if not os.path.exists(path):
            continue
        # A loaded model keeps its files open; unloading first lets Windows delete them too.
        try:
            llama_call("POST", base + "/models/unload", key, {"model": name}, MINUTE)
        except Refused:
            pass
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        attempt(base + "/models?reload=1", llama_headers(key), 30)
        return True
    return False


def llama_log(port: int, size: int) -> str:
    """The log of the llama-server agent-machine started (servers.py writes it)."""
    if port != llama_server_port():
        raise Refused(f"only the llama-server agent-machine starts (on port {llama_server_port()}) has a log here")
    try:
        return tail(settings.home() / "llamacpp.log", size)
    except FileNotFoundError:
        return "No log yet: agent-machine writes one when it starts llama.cpp."


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
    # A "." or ".." part would be resolved into another path of the model server: keep it a name.
    return "/".join(
        part.replace(".", "%2E") if part in (".", "..") else quote(part, safe="$&+:=@")  # what Go's url.PathEscape keeps
        for part in name.strip().split("/")
    )


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


# OpenAI-compatible servers (kind "api", api.go): only their model list is read.

def api_status(base: str) -> Dict[str, Any]:
    """An OpenAI-compatible server's models, from /v1/models."""
    listed, status, error = attempt(base + "/v1/models")
    if error is not None:
        return {"online": False, "error": str(error), "models": []}
    if status >= 400:
        return {"online": False, "error": f"The OpenAI-compatible server returned {status}", "models": []}
    data = obj(listed).get("data")
    if not isinstance(data, list):
        return {"online": False, "error": "It answered, but not with an OpenAI model list.", "models": []}
    models = []
    for raw in data:
        item = obj(raw)
        model_id = text(item.get("id"))
        if not model_id:
            continue
        model: Dict[str, Any] = {"id": model_id, "owned_by": text(item.get("owned_by"))}
        # vLLM says max_model_len, LM Studio loaded_context_length; others context_length.
        for key in ("context_length", "max_model_len", "loaded_context_length"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                model["context_length"] = value
                break
        models.append(model)
    return {"online": True, "version": "", "models": models}


# Model servers running here, for the hello (presence/providers.go).

def api_ports() -> List[int]:
    """The ports named in AI_PROXY_API_PORTS; entries that aren't ports are ignored."""
    ports = []
    for entry in os.environ.get(API_PORTS_ENV, "").split(","):
        entry = entry.strip()
        # Go's Atoi: an optional sign and ASCII digits only.
        if re.fullmatch(r"[+-]?[0-9]+", entry) and 0 < int(entry) <= 65535:
            ports.append(int(entry))
    return ports


def openai_models(http: httpx.Client, port: int) -> Optional[int]:
    """How many models an OpenAI-compatible server lists, else None. Many unrelated programs listen
    on these ports (8000 above all), so only a 200 with an OpenAI model list counts:
    {"data": [...]}, each item an object with an id."""
    try:
        response = http.get(f"http://127.0.0.1:{port}/v1/models")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = obj(response.json()).get("data")
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        model_id = obj(item).get("id")
        if not isinstance(model_id, str) or not model_id:
            return None
    return len(data)

def detect() -> List[Dict[str, Any]]:
    """Asks each default port on loopback whether its model server answers, then the
    OpenAI-compatible servers' ports (API_PROBES and AI_PROXY_API_PORTS) not already taken."""
    found: List[Dict[str, Any]] = []
    with httpx.Client(trust_env=False, timeout=1.5) as http:
        for kind, path, port in PROBES:
            if kind == "llamacpp":
                port = llama_server_port()
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
        candidates = list(API_PROBES) + [("openai", port) for port in api_ports()]
        for flavor, port in candidates:
            if any(provider["port"] == port for provider in found):
                continue
            count = openai_models(http, port)
            if count is not None:
                found.append({"type": "api", "port": port, "models": count, "flavor": flavor})
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
        # Each part answers None where this system can't tell, so one that fails never costs the sample.
        swap_used, swap_total = _part(swap, (None, None))
        value.update(
            uptime_seconds=_part(uptime, None), load_average=_part(load_average, None),
            swap={"used": swap_used, "total": swap_total}, network=_part(network, None), disks=_part(lambda: disks(folders()), []),
            process={"pid": os.getpid(), "rss": _part(rss, None), "threads": threading.active_count(), "started_at": PROCESS_STARTED},
            agent=activity.snapshot(),
        )
        _last.update(at=time.monotonic(), value=value)
        return value


def _part(read: Callable[[], Any], otherwise: Any) -> Any:
    try:
        return read()
    except Exception:  # noqa: BLE001 - a part this system can't tell is left empty, never raised into the sample
        return otherwise


# Stands for when agent-machine started: this module loads with the program.
PROCESS_STARTED = time.time()


class Activity:
    """Counts what this agent-machine has done since it started, for the sample's "agent" part.
    presence, which holds the fleet connection and the tunnels, reports into it from its event loop
    while samples are taken on worker threads, hence the lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connected_since: Optional[float] = None
        self.counts = {"connects": 0, "requests": 0, "tunnels_open": 0, "tunnels_total": 0, "tunnel_bytes_in": 0, "tunnel_bytes_out": 0}

    def add(self, name: str, amount: int = 1) -> None:
        with self.lock:
            self.counts[name] += amount

    def connected(self) -> None:
        """The server welcomed a fleet connection."""
        with self.lock:
            self.connected_since = time.time()
            self.counts["connects"] += 1

    def disconnected(self) -> None:
        with self.lock:
            self.connected_since = None

    def request(self) -> None:
        self.add("requests")

    def tunnel_opened(self) -> None:
        """A tunnel reached its model server."""
        with self.lock:
            self.counts["tunnels_open"] += 1
            self.counts["tunnels_total"] += 1

    def tunnel_closed(self) -> None:
        self.add("tunnels_open", -1)

    def tunnel_in(self, amount: int) -> None:
        """Bytes from the server into a model server."""
        self.add("tunnel_bytes_in", amount)

    def tunnel_out(self, amount: int) -> None:
        self.add("tunnel_bytes_out", amount)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"connected_since": self.connected_since, **self.counts}


activity = Activity()


def _psutil(read: Callable[[Any], Any]) -> Any:
    """What psutil tells, if it's installed: only asked where the Go agent-machine's own way
    (/proc, the Windows API, the system's tools) has no answer."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return read(psutil)
    except Exception:  # noqa: BLE001
        return None


def _output(*command: str) -> Optional[str]:
    """What a system tool prints, on the systems without /proc; one that hangs mustn't hold the sample."""
    try:
        output = subprocess.run(list(command), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return output.stdout if output.returncode == 0 else None


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as file:
            return file.read()
    except OSError:
        return None


def uptime() -> Optional[float]:
    """Seconds since the computer started."""
    if sys.platform == "win32":
        import ctypes

        tick = ctypes.windll.kernel32.GetTickCount64
        tick.restype = ctypes.c_ulonglong
        return math.floor(tick() / 100) / 10
    found: Optional[float] = None
    if linux():
        try:
            found = float((_read("/proc/uptime") or "").split()[0])
        except (IndexError, ValueError):
            found = None
    else:
        boot = parse_boot_time(_output("sysctl", "-n", "kern.boottime") or "")
        found = math.floor((time.time() - boot) * 10) / 10 if boot else None
    if found is None:
        found = _psutil(lambda psutil: math.floor((time.time() - psutil.boot_time()) * 10) / 10)
    return found


def parse_boot_time(text: str) -> Optional[float]:
    """The second the system started, from sysctl's kern.boottime:
    "{ sec = 1700000000, usec = 0 } Tue Nov 14 22:13:20 2023"."""
    _, found, rest = text.partition("sec = ")
    try:
        value = float(rest.partition(",")[0].strip()) if found else 0.0
    except ValueError:
        return None
    return value if value > 0 else None


def load_average() -> Optional[List[float]]:
    """The 1, 5 and 15 minute load averages; Windows keeps none."""
    if sys.platform == "win32":
        return None
    text = _read("/proc/loadavg") if linux() else _output("sysctl", "-n", "vm.loadavg")
    found = parse_load(text or "")
    if found is None:
        try:
            found = [float(value) for value in os.getloadavg()]
        except (AttributeError, OSError):
            found = None
    return found


def parse_load(text: str) -> Optional[List[float]]:
    """/proc/loadavg ("0.52 0.41 0.30 1/234 5678") or sysctl's vm.loadavg ("{ 2.10 1.95 1.80 }")."""
    fields = text.strip().strip("{}").split()
    try:
        return [float(value) for value in fields[:3]] if len(fields) >= 3 else None
    except ValueError:
        return None


def swap() -> Tuple[Optional[int], Optional[int]]:
    """The swap in use and in all; both 0 on a system without any."""
    found: Optional[Tuple[Optional[int], Optional[int]]] = None
    if sys.platform == "win32":
        found = windows_swap()
    elif linux():
        values = meminfo()
        if values is not None:
            total, free = values.get("SwapTotal", 0), values.get("SwapFree", 0)
            found = (None, total) if free > total else (total - free, total)
    elif sys.platform == "darwin":
        text = _output("sysctl", "-n", "vm.swapusage")
        found = parse_swap_usage(text) if text is not None else None
    if found is None:
        found = _psutil(lambda psutil: (int(psutil.swap_memory().used), int(psutil.swap_memory().total)))
    return found or (None, None)


def windows_swap() -> Optional[Tuple[int, int]]:
    """The page files' use and size. GlobalMemoryStatusEx only has the commit limit, which counts
    memory too, so the page files themselves are asked for: NtQuerySystemInformation's
    SYSTEM_PAGEFILE_INFORMATION (class 18) has, for each, an offset to the next entry, then its size
    and its use, in pages."""
    import ctypes
    import mmap
    import struct

    buffer = ctypes.create_string_buffer(16 * 1024)
    length = ctypes.c_ulong(0)
    if ctypes.windll.ntdll.NtQuerySystemInformation(18, buffer, len(buffer), ctypes.byref(length)) != 0:
        return None
    data, offset, in_use, size = buffer.raw[:length.value], 0, 0, 0
    # No page file at all answers nothing: 0 of 0.
    while offset + 12 <= len(data):
        following, total, used = struct.unpack_from("<III", data, offset)
        size, in_use = size + total, in_use + used
        if not following:
            break
        offset += following
    return in_use * mmap.PAGESIZE, size * mmap.PAGESIZE


def parse_swap_usage(text: str) -> Tuple[Optional[int], Optional[int]]:
    """macOS's vm.swapusage: "total = 2048.00M  used = 1073.25M  free = 974.75M  (encrypted)"."""
    fields = text.split()

    def amount(name: str) -> Optional[int]:
        for index in range(len(fields) - 2):
            if fields[index] == name and fields[index + 1] == "=":
                value = fields[index + 2]
                scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(value[-1:], 1)
                try:
                    return int(float(value.rstrip("KMG")) * scale)
                except ValueError:
                    return None
        return None

    return amount("used"), amount("total")


NETWORK_KEYS = ("bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errors", "interfaces")


def network() -> Optional[Dict[str, int]]:
    """Traffic since boot, summed over the interfaces that aren't loopback."""
    if sys.platform == "win32":
        found = windows_network()
    elif linux():
        text = _read("/proc/net/dev")
        found = parse_net_dev(text) if text is not None else None
    else:
        text = _output("netstat", "-ibn")
        found = parse_netstat(text) if text is not None else None
    return found if found is not None else _psutil(psutil_network)


def psutil_network(psutil: Any) -> Dict[str, int]:
    totals = dict.fromkeys(NETWORK_KEYS, 0)
    for name, counters in psutil.net_io_counters(pernic=True).items():
        if name == "lo" or name.startswith("lo0") or name.lower().startswith("loopback"):
            continue
        totals["bytes_sent"] += counters.bytes_sent
        totals["bytes_recv"] += counters.bytes_recv
        totals["packets_sent"] += counters.packets_sent
        totals["packets_recv"] += counters.packets_recv
        totals["errors"] += counters.errin + counters.errout
        totals["interfaces"] += 1
    return totals


def parse_net_dev(text: str) -> Optional[Dict[str, int]]:
    """Linux's /proc/net/dev: after two header lines, "name: " and sixteen counters, eight received
    then eight sent (bytes, packets, errors, ...)."""
    totals, found = dict.fromkeys(NETWORK_KEYS, 0), False
    for line in text.split("\n"):
        name, colon, rest = line.partition(":")
        fields = rest.split()
        if not colon or len(fields) < 11:
            continue
        found = True
        if name.strip() == "lo":
            continue
        numbers = [_count(field) for field in fields]
        totals["bytes_recv"] += numbers[0]
        totals["packets_recv"] += numbers[1]
        totals["errors"] += numbers[2] + numbers[10]
        totals["bytes_sent"] += numbers[8]
        totals["packets_sent"] += numbers[9]
        totals["interfaces"] += 1
    return totals if found else None


def _count(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def parse_netstat(text: str) -> Optional[Dict[str, int]]:
    """`netstat -ib` on macOS and the BSDs. Each interface has a row per address; only its <Link#n>
    row counts, or its traffic would be added once per address. A row without an address has one
    field fewer, and FreeBSD has an Idrop column macOS doesn't, so columns are found by their
    header's name, counted from the end."""
    lines = text.strip().split("\n")
    header = lines[0].split()
    from_end = {name: len(header) - index for index, name in enumerate(header)}
    if any(name not in from_end for name in ("Ipkts", "Ierrs", "Ibytes", "Opkts", "Oerrs", "Obytes")):
        return None
    totals, seen = dict.fromkeys(NETWORK_KEYS, 0), set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < len(header) - 1 or not fields[2].startswith("<Link#"):
            continue
        name = fields[0].rstrip("*")  # a trailing * marks an interface that's down
        if name.startswith("lo") or name in seen:
            continue
        seen.add(name)

        def number(column: str) -> int:
            return _count(fields[len(fields) - from_end[column]])

        totals["bytes_recv"] += number("Ibytes")
        totals["packets_recv"] += number("Ipkts")
        totals["bytes_sent"] += number("Obytes")
        totals["packets_sent"] += number("Opkts")
        totals["errors"] += number("Ierrs") + number("Oerrs")
        totals["interfaces"] += 1
    return totals


# The layout of Windows' MIB_IF_TABLE2 (a count, then rows from byte 8) and MIB_IF_ROW2, read by
# offset as the Go agent-machine does.
IF_ROW_SIZE = 1352
IF_ROW_TYPE, IF_ROW_FLAGS = 1128, 1152
IF_ROW_IN_OCTETS, IF_ROW_IN_UCAST, IF_ROW_IN_NUCAST, IF_ROW_IN_ERRORS = 1208, 1216, 1224, 1240
IF_ROW_OUT_OCTETS, IF_ROW_OUT_UCAST, IF_ROW_OUT_NUCAST, IF_ROW_OUT_ERRORS = 1280, 1288, 1296, 1312
IF_TYPE_LOOPBACK = 24
# InterfaceAndOperStatusFlags: a filter driver's layer over an adapter, and "has a connector".
IF_FLAG_FILTER, IF_FLAG_CONNECTOR = 0x02, 0x04


def sum_if_table(table: bytes) -> Optional[Dict[str, int]]:
    """Adds up GetIfTable2's rows. Windows lists every adapter several times (once per filter driver
    layered on it) and virtual adapters whose traffic also crosses a real one, so only interfaces
    with a connector count: each real adapter once."""
    import struct

    if len(table) < 8:
        return None
    totals = dict.fromkeys(NETWORK_KEYS, 0)
    for index in range(struct.unpack_from("<I", table)[0]):
        start = 8 + index * IF_ROW_SIZE
        if start + IF_ROW_SIZE > len(table):
            return None

        def number(offset: int) -> int:
            return struct.unpack_from("<Q", table, start + offset)[0]

        flags = table[start + IF_ROW_FLAGS]
        if struct.unpack_from("<I", table, start + IF_ROW_TYPE)[0] == IF_TYPE_LOOPBACK or not flags & IF_FLAG_CONNECTOR or flags & IF_FLAG_FILTER:
            continue
        totals["bytes_recv"] += number(IF_ROW_IN_OCTETS)
        totals["packets_recv"] += number(IF_ROW_IN_UCAST) + number(IF_ROW_IN_NUCAST)
        totals["bytes_sent"] += number(IF_ROW_OUT_OCTETS)
        totals["packets_sent"] += number(IF_ROW_OUT_UCAST) + number(IF_ROW_OUT_NUCAST)
        totals["errors"] += number(IF_ROW_IN_ERRORS) + number(IF_ROW_OUT_ERRORS)
        totals["interfaces"] += 1
    return totals


def windows_network() -> Optional[Dict[str, int]]:
    import ctypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    iphlpapi.GetIfTable2.restype = ctypes.c_ulong
    iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
    iphlpapi.FreeMibTable.restype = None
    table = ctypes.c_void_p()
    if iphlpapi.GetIfTable2(ctypes.byref(table)) != 0 or not table.value:
        return None
    try:
        count = ctypes.c_ulong.from_address(table.value).value
        return sum_if_table(ctypes.string_at(table.value, 8 + count * IF_ROW_SIZE))
    finally:
        iphlpapi.FreeMibTable(table)


def rss() -> Optional[int]:
    """agent-machine's resident memory."""
    found: Optional[int] = None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "quota_peak_paged", "quota_paged",
                                                     "quota_peak_non_paged", "quota_non_paged", "pagefile", "peak_pagefile")]

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.size = ctypes.sizeof(Counters)
        if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.size):
            found = int(counters.working_set)
    elif linux():
        try:
            # The second number of /proc/self/statm, in pages.
            found = int((_read("/proc/self/statm") or "").split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (IndexError, ValueError, OSError):
            found = None
    else:
        try:
            found = int((_output("ps", "-o", "rss=", "-p", str(os.getpid())) or "").strip()) * 1024  # ps prints KiB
        except ValueError:
            found = None
    return found if found is not None else _psutil(lambda psutil: int(psutil.Process().memory_info().rss))


def folders() -> List[Tuple[str, str]]:
    """Where agent-machine keeps things, and what the console calls each: its settings, the large
    files, and the launch folders. The roots are read straight from machine.json: Machine.load
    writes the file when it's missing, which a sample never should."""
    found = [("Settings", str(settings.home())), ("Data", str(settings.data()))]
    try:
        data = json.loads(settings.machine_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    roots = data.get("roots") if isinstance(data, dict) else None
    for root in roots if isinstance(roots, list) else []:
        if isinstance(root, str):
            found.append(("Launch folder", root))
    return found


def disks(folders: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """The filesystem under each folder, each once, with what it holds."""
    found: List[Dict[str, Any]] = []
    for label, folder in folders:
        path = existing(folder)
        if not path:
            continue
        try:
            mount = mountpoint(path)
            usage = shutil.disk_usage(mount if sys.platform == "win32" else path)
        except OSError:
            continue
        if not mount or usage.total <= 0:
            continue
        disk = next((other for other in found if other["mountpoint"] == mount), None)
        if disk is None:
            disk = {"mountpoint": mount, "total": usage.total, "used": usage.used, "free": usage.free,
                    "percent": math.floor(usage.used * 1000 / usage.total) / 10, "holds": []}
            found.append(disk)
        if label not in disk["holds"]:
            disk["holds"].append(label)
    return found


def existing(path: str) -> str:
    """path, or the nearest folder above it that exists, with links resolved: a folder
    agent-machine hasn't made yet still sits on a filesystem."""
    path = os.path.abspath(path)
    while True:
        if os.path.exists(path):
            return os.path.realpath(path)
        parent = os.path.dirname(path)
        if parent == path:
            return ""
        path = parent


def mountpoint(path: str) -> str:
    """Where path's filesystem is mounted: the volume on Windows, else the highest folder above it
    on the same device."""
    if sys.platform == "win32":
        import ctypes

        volume = ctypes.create_unicode_buffer(261)
        if not ctypes.windll.kernel32.GetVolumePathNameW(ctypes.c_wchar_p(path), volume, len(volume)):
            return ""
        return volume.value
    while True:
        parent = os.path.dirname(path)
        try:
            if parent == path or os.stat(parent).st_dev != os.stat(path).st_dev:
                return path
        except OSError:
            return path
        path = parent


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
    values = meminfo()
    if values is None:
        return 0, 0
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    if not available or available > total:
        return 0, total
    return total - available, total


def meminfo() -> Optional[Dict[str, int]]:
    """/proc/meminfo's amounts, in bytes."""
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
        return None
    return values


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
