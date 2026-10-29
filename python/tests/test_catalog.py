"""The catalogue searches (agent_py/machine/catalog.py), as the Go agent-machine's TestCatalogParsers and TestSearchCatalogs check them."""

from __future__ import annotations

import httpx

from agent_py.machine import catalog, providers

LIBRARY_PAGE = """<ul><li><a href="/library/qwen3" class="group w-full">
<p class="max-w-lg" title="Qwen3 &amp; friends">x</p>
<span class="text-indigo-600 x">tools</span><span class="text-indigo-600">thinking</span>
<span class="text-blue-600">8B</span><span class="text-blue-600">8b</span><span class="text-blue-600">32B</span>
<span title="1,234 downloads">1.2K</span></a></li>
<li><a href="/library/user/x" class="group">skip</a></li></ul>"""


def test_parsers():
    [model] = catalog.parse_library(LIBRARY_PAGE)
    assert model == {"name": "qwen3", "description": "Qwen3 & friends", "capabilities": ["tools", "thinking"], "sizes": ["8b", "32b"], "pulls": 1234}
    for stem, want in {"Qwen3-8B-UD-Q4_K_XL": "UD-Q4_K_XL", "model-q8_0": "Q8_0", "big-BF16-00001-of-00002": "BF16", "readme": "", "Qwen3-8B": ""}.items():
        assert catalog.quant_from_filename(stem) == want, stem
    quants = catalog.parse_quants([
        {"type": "file", "path": "Q8/m-Q8_0-00001-of-00002.gguf", "lfs": {"size": 50}},
        {"type": "file", "path": "Q8/m-Q8_0-00002-of-00002.gguf", "lfs": {"size": 50}},
        {"type": "file", "path": "m-Q4_K_M.gguf", "size": 40},
        {"type": "file", "path": "mmproj-F16.gguf", "size": 1},
        {"type": "directory", "path": "Q8"},
    ])
    assert quants == [{"quant": "Q4_K_M", "size": 40, "split": False}, {"quant": "Q8_0", "size": 100, "split": True}]
    tags = catalog.parse_hub_tags({"results": [
        {"name": "8B-Q8_0", "full_size": 9}, {"name": "8B-Q4_K_M", "full_size": 5},
        {"name": "latest", "full_size": 5}, {"name": "8B-safetensors", "full_size": 16},
    ]})
    assert [tag["tag"] for tag in tags] == ["latest", "8B-Q4_K_M", "8B-Q8_0", "8B-safetensors"]
    assert tags[3]["engine"] == "vllm" and tags[1]["engine"] is None and catalog.tag_engine("4B-mlx") == "mlx"


def test_searches_through_the_machine():
    hub_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search":
            if request.url.params.get("q") != "qwen" or "agent-machine" not in request.headers["user-agent"]:
                return httpx.Response(400)
            return httpx.Response(200, text=LIBRARY_PAGE)
        if path == "/api/models":
            return httpx.Response(200, json=[{"id": "unsloth/Qwen3-GGUF", "downloads": 10}])
        if path == "/api/models/gated/repo/tree/main":
            return httpx.Response(403)
        if path == "/v2/repositories/ai/":
            hub_calls.append(request.url.params.get("name"))
            return httpx.Response(200, json={"results": [{"name": "qwen3", "namespace": "ai", "pull_count": 7}]})
        if path == "/v2/repositories/ai/qwen3/tags":
            return httpx.Response(200, json={"results": [{"name": "latest"}]})
        return httpx.Response(404)

    manager = providers.Manager()
    manager.catalogs = catalog.Catalogs(lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://catalog"))
    manager.catalogs.library_url, manager.catalogs.hugging_face_url, manager.catalogs.docker_hub_url = "http://catalog/search", "http://catalog/api/models", "http://catalog/v2"

    def search(name: str, query: str = "", repo: str = "") -> dict:
        answer = manager.handle({"op": "search", "request": "r", "catalog": name, "query": query, "repo": repo})
        assert answer["ok"] is True and answer["type"] == "provider_result", answer
        return answer["result"]

    result = search("ollama", "qwen")
    assert len(result["models"]) == 1 and result["note"] is None
    assert search("huggingface", "qwen")["repos"][0]["repo"] == "unsloth/Qwen3-GGUF"
    assert search("huggingface")["note"]
    gated = manager.handle({"op": "search", "catalog": "huggingface-quants", "repo": "gated/repo"})
    assert gated["ok"] is False and "gated" in gated["error"]
    search("docker-hub", "Qwen")
    assert search("docker-hub", "qwen")["models"][0]["name"] == "ai/qwen3" and hub_calls == ["qwen"]
    assert len(search("docker-hub-tags", repo="ai/qwen3")["tags"]) == 1
    assert manager.handle({"op": "search", "catalog": "docker-hub-tags", "repo": "../etc"})["ok"] is False
    assert manager.handle({"op": "search", "catalog": "pypi"})["ok"] is False
