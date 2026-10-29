"""The model catalogues the console searches to pull from: ollama.com's library, GGUF
repositories on Hugging Face (both for Ollama) and Docker's curated models on Docker Hub
(for Docker Model Runner). Searched from the machine that will pull the model.

The same searches, answers and messages as the Go agent-machine's catalog.go.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

LIBRARY_URL = "https://ollama.com/search"
HUGGING_FACE_URL = "https://huggingface.co/api/models"
DOCKER_HUB_URL = "https://hub.docker.com/v2"
HUB_NAMESPACE = "ai"
HUB_CACHE_SECONDS = 600
SEARCH_AGENT = "Mozilla/5.0 (compatible; agent-machine model search)"
MAX_BODY = 16 << 20

# ollama.com/search has no JSON API, so results are parsed from the page. Each model is an
# <a href="/library/NAME" class="group ..."> card holding the description (in a <p title>),
# capability pills (indigo) and size pills (blue).
LIBRARY_CARD = re.compile(r'<a href="/library/([^"?#]+)"\s+class="group[^"]*">(.*?)</a>', re.S)
LIBRARY_DESC = re.compile(r'<p[^>]*\btitle="([^"]*)"')
LIBRARY_CAP = re.compile(r"text-indigo-600[^>]*>([^<]+)<")
LIBRARY_SIZE = re.compile(r"text-blue-600[^>]*>([^<]+)<")
LIBRARY_DOWNLOADS = re.compile(r'title="([\d,]+)\s+downloads?"')

# The quantization at the end of a GGUF filename: Qwen3-8B-UD-Q4_K_XL -> UD-Q4_K_XL.
HF_QUANT = re.compile(r"(?:^|[^A-Za-z0-9])((?:UD-)?(?:I?Q\d+(?:_[A-Za-z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?))$", re.I)
HF_SHARD = re.compile(r"-\d{5}-of-\d{5}$")
HUB_REPO = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")


class Failed(Exception):
    """A search that couldn't be answered; its text is shown in the console."""


def external() -> httpx.Client:
    """Catalogues get a plain client, without the server's headers or a proxy from the environment, like the Go one."""
    return httpx.Client(trust_env=False, timeout=20, follow_redirects=True)


class Catalogs:
    """The searches. The URLs and the client are replaceable for tests."""

    def __init__(self, client: Callable[[], httpx.Client] = external) -> None:
        self.client = client
        self.library_url = LIBRARY_URL
        self.hugging_face_url = HUGGING_FACE_URL
        self.docker_hub_url = DOCKER_HUB_URL
        self._hub: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._hub_lock = threading.Lock()

    def search(self, catalog: str, query: str, repo: str) -> Dict[str, Any]:
        query = (query or "").strip()
        if catalog == "ollama":
            return self.search_library(query, 30)
        if catalog == "huggingface":
            return self.search_hugging_face(query, 25)
        if catalog == "huggingface-quants":
            return self.hugging_face_quants(repo or "")
        if catalog == "docker-hub":
            return self.search_docker_hub(query, 60)
        if catalog == "docker-hub-tags":
            return self.docker_hub_tags(repo or "")
        raise Failed(f"unknown catalogue {json.dumps(catalog)}")

    def fetch(self, address: str, params: Optional[Dict[str, str]] = None) -> Tuple[bytes, int]:
        """GETs a catalogue page; the status is the answer's."""
        with self.client() as http, http.stream("GET", address, params=params or None, headers={"User-Agent": SEARCH_AGENT}) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= MAX_BODY:
                    break
            return bytes(body[:MAX_BODY]), response.status_code

    def fetch_json(self, address: str, params: Optional[Dict[str, str]], who: str) -> Any:
        try:
            data, status = self.fetch(address, params)
            if status >= 400:
                raise ValueError(f"status {status}")
            return json.loads(data)
        except (httpx.HTTPError, ValueError) as error:
            raise Failed(f"Couldn't reach {who}: {error}")

    def search_library(self, query: str, limit: int) -> Dict[str, Any]:
        try:
            page, status = self.fetch(self.library_url, {"q": query})
            if status >= 400:
                raise ValueError(f"status {status}")
        except (httpx.HTTPError, ValueError) as error:
            raise Failed(f"Couldn't reach ollama.com: {error}")
        models = parse_library(page.decode("utf-8", "replace"))[:limit]
        note = None if models else "No models found. Try a different search, or paste an exact tag into a machine's Pull box."
        return {"query": query, "models": models, "note": note}

    def search_hugging_face(self, query: str, limit: int) -> Dict[str, Any]:
        if not query:
            return {"query": "", "repos": [], "note": "Type to search Hugging Face for GGUF models."}
        params = {"search": query, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": str(limit)}
        items = self.fetch_json(self.hugging_face_url, params, "Hugging Face")
        repos = [
            {"repo": item["id"], "downloads": item.get("downloads"), "likes": item.get("likes")}
            for item in (items if isinstance(items, list) else []) if isinstance(item, dict) and text(item.get("id"))
        ]
        note = None if repos else "No GGUF repositories found. Try a different search."
        return {"query": query, "repos": repos, "note": note}

    def hugging_face_quants(self, repo: str) -> Dict[str, Any]:
        repo = repo.strip().strip("/")
        if not repo or repo.count("/") != 1:
            raise Failed("Use the form user/repo")
        try:
            data, status = self.fetch(f"{self.hugging_face_url}/{repo}/tree/main", {"recursive": "1"})
        except httpx.HTTPError as error:
            raise Failed(f"Couldn't reach Hugging Face: {error}")
        if status in (401, 403):
            raise Failed("This repo is gated or private. Accept its licence on huggingface.co first.")
        if status == 404:
            raise Failed("Repo not found on Hugging Face.")
        if status >= 400:
            raise Failed(f"Hugging Face returned {status}")
        try:
            files = json.loads(data)
        except ValueError:
            raise Failed("Unexpected response from Hugging Face")
        quants = parse_quants(files)
        note = None if quants else "No GGUF files here. Ollama can only pull GGUF models; look for a repo ending in -GGUF."
        return {"repo": repo, "quants": quants, "note": note}

    def search_docker_hub(self, query: str, limit: int) -> Dict[str, Any]:
        """Docker's curated models (the ai/ namespace), most pulled first; with a query, those named
        like it. Docker Hub only gives anonymous clients the first 100 results, so the name filter
        runs on its side. Cached for ten minutes."""
        key = query.lower()
        with self._hub_lock:
            cached = self._hub.get(key)
        if cached is not None and time.monotonic() - cached[0] <= HUB_CACHE_SECONDS:
            models = cached[1]
        else:
            params = {"page_size": "100", "ordering": "pull_count"}
            if key:
                params["name"] = key
            page = self.fetch_json(f"{self.docker_hub_url}/repositories/{HUB_NAMESPACE}/", params, "Docker Hub")
            models = parse_hub_repositories(page)
            with self._hub_lock:
                if len(self._hub) > 64:
                    self._hub = {}
                self._hub[key] = (time.monotonic(), models)
        models = models[:limit]
        note = None if models else "No Docker Hub models have that in their name. Pull any other model by name, such as hf.co/<user>/<repo>."
        return {"query": query, "models": models, "note": note}

    def docker_hub_tags(self, repo: str) -> Dict[str, Any]:
        repo = repo.strip().strip("/").lower()
        if not HUB_REPO.match(repo):
            raise Failed("Use the form namespace/model, such as ai/qwen3")
        try:
            data, status = self.fetch(f"{self.docker_hub_url}/repositories/{repo}/tags", {"page_size": "100"})
        except httpx.HTTPError as error:
            raise Failed(f"Couldn't reach Docker Hub: {error}")
        if status == 404:
            raise Failed(f"{repo} isn't on Docker Hub.")
        if status >= 400:
            raise Failed(f"Docker Hub returned {status}")
        try:
            page = json.loads(data)
        except ValueError:
            raise Failed("Unexpected response from Docker Hub")
        return {"repo": repo, "tags": parse_hub_tags(page)}


def text(value: Any) -> str:
    """A JSON value as the Go agent-machine's str() reads it: strings, numbers and booleans."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return format(value, "g") if isinstance(value, float) else str(value)
    return ""


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0
    return 0


def parse_library(page: str) -> List[Dict[str, Any]]:
    """The model cards on an ollama.com/search page."""
    models: List[Dict[str, Any]] = []
    seen = set()
    for match in LIBRARY_CARD.finditer(page):
        name, block = match.group(1).strip(), match.group(2)
        if not name or "/" in name or name in seen:
            continue
        seen.add(name)
        found = LIBRARY_DESC.search(block)
        description = html.unescape(found.group(1)).strip() if found else ""
        capabilities = [html.unescape(value).strip() for value in LIBRARY_CAP.findall(block)]
        sizes: List[str] = []
        for value in LIBRARY_SIZE.findall(block):
            size = value.strip().lower()
            if size and size not in sizes:
                sizes.append(size)
        pulls = None
        found = LIBRARY_DOWNLOADS.search(block)
        if found:
            try:
                pulls = int(found.group(1).replace(",", ""))
            except ValueError:
                pass
        models.append({"name": name, "description": description, "capabilities": capabilities, "sizes": sizes, "pulls": pulls})
    return models


def quant_from_filename(stem: str) -> str:
    """'Qwen3-8B-UD-Q4_K_XL' -> 'UD-Q4_K_XL', or '' without a quantization."""
    match = HF_QUANT.search(HF_SHARD.sub("", stem))
    return match.group(1).upper() if match else ""


def parse_quants(files: Any) -> List[Dict[str, Any]]:
    """The GGUF quantizations in a Hugging Face repo listing, summed up, smallest first."""
    quants: Dict[str, Dict[str, Any]] = {}
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or text(item.get("type")) != "file":
            continue
        path = text(item.get("path"))
        name = path.rsplit("/", 1)[-1]
        lowered = name.lower()
        if not lowered.endswith(".gguf") or "mmproj" in lowered:
            continue
        stem = name[:-5]
        quant = quant_from_filename(stem)
        if not quant:
            continue
        entry = quants.setdefault(quant, {"quant": quant, "size": 0, "split": False})
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        size = number(lfs.get("size")) or number(item.get("size"))
        entry["size"] += int(size)
        entry["split"] = entry["split"] or bool(HF_SHARD.search(stem))
    return sorted(quants.values(), key=lambda entry: entry["size"])


def parse_hub_repositories(page: Any) -> List[Dict[str, Any]]:
    results = page.get("results") if isinstance(page, dict) else None
    models = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not text(item.get("name")):
            continue
        namespace = text(item.get("namespace")) or HUB_NAMESPACE
        models.append({
            "name": f"{namespace}/{text(item.get('name'))}", "description": text(item.get("description")),
            "pulls": item.get("pull_count"), "stars": item.get("star_count"), "updated": item.get("last_updated"),
        })
    return models


def tag_engine(tag: str) -> Optional[str]:
    """The engine a Docker Hub tag needs beyond llama.cpp: vllm for safetensors weights, mlx for
    Apple MLX ones; None for llama.cpp."""
    lowered = tag.lower()
    if "safetensors" in lowered or lowered.endswith("vllm"):
        return "vllm"
    if "mlx" in lowered:
        return "mlx"
    return None


def parse_hub_tags(page: Any) -> List[Dict[str, Any]]:
    """A model's tags, latest first, then smallest."""
    results = page.get("results") if isinstance(page, dict) else None
    tags = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        name = text(item.get("name"))
        if name:
            tags.append({"tag": name, "size": item.get("full_size"), "engine": tag_engine(name), "updated": item.get("last_updated")})
    tags.sort(key=lambda tag: (tag["tag"] != "latest", number(tag["size"])))
    return tags
