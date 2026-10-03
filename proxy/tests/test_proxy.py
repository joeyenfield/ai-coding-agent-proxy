from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from agent_proxy.config import Backend, Settings
from agent_proxy.app import create_app


def make_app(tmp_path: Path, handler, **settings_args):
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")}, **settings_args)
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    old_client = app.state.runtime.client
    app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app, client, old_client


def close_app(app, client, old_client):
    import asyncio

    asyncio.run(app.state.runtime.client.aclose())
    app.state.runtime.client = old_client
    client.__exit__(None, None, None)


def test_openai_passthrough_stream_records_telemetry_and_trace(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n' + b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n' + b'data: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    app, client, old = make_app(tmp_path, handler, model_defaults={"translate_openai": False})
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}).json()
        with client.stream("POST", f"/session/{session['session_id']}/v1/chat/completions", json={"model": "model", "stream": True, "messages": []}) as response:
            assert response.status_code == 200
            assert "hello" in response.read().decode()
        telemetry = client.get(f"/api/sessions/{session['session_id']}/telemetry").text
        record = json.loads(telemetry)
        assert record["request_id"] == "000001"
        assert record["input_tokens"] == 4
        assert record["output_tokens"] == 2
        assert record["ttft_ms"] is not None
        trace = client.get(f"/api/sessions/{session['session_id']}/traces/000001").json()
        assert trace["metadata"]["model"] == "model"
        assert trace["request"] == {"model": "model", "stream": True, "messages": []}
        assert trace["response"] == [
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
        ]
    finally:
        close_app(app, client, old)


def test_openai_chat_is_translated_to_native_ollama_with_profile(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.update(json.loads(request.content))
        lines = [
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 2, "eval_duration": 1_000_000_000},
        ]
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content="".join(json.dumps(line) + "\n" for line in lines).encode())

    app, client, old = make_app(tmp_path, handler, model_defaults={"keep_alive": "30m", "options": {"num_ctx": 65536}})
    try:
        session = client.post("/api/sessions", json={"client": "qwen", "backend": "test", "trace": True}).json()
        with client.stream("POST", f"/session/{session['session_id']}/v1/chat/completions", json={
            "model": "qwen3.6:35b", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Hi"}],
        }) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
        assert seen["options"] == {"num_ctx": 65536} and seen["keep_alive"] == "30m"
        assert events[-1] == "[DONE]"
        chunks = [json.loads(event) for event in events[:-1]]
        assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"]) == "Hello"
        assert chunks[-1]["usage"]["completion_tokens"] == 2
        record = json.loads(client.get(f"/api/sessions/{session['session_id']}/telemetry").text)
        assert record["input_tokens"] == 7 and record["output_tokens"] == 2 and record["generation_tps"] == 2
        trace = client.get(f"/api/sessions/{session['session_id']}/traces/000001").json()
        assert trace["upstream_request"]["options"]["num_ctx"] == 65536
    finally:
        close_app(app, client, old)


def test_agents_and_models_endpoints(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:35b", "details": {"parameter_size": "36.0B"}}]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:35b", "context_length": 65536}]})
        return httpx.Response(200, json={"version": "0.35.1"})

    app, client, old = make_app(tmp_path, handler)
    try:
        agents = {agent["id"]: agent for agent in client.get("/api/agents").json()}
        assert agents["qwen"]["protocol"] == "openai-chat"
        prepared = client.post("/api/agents/qwen/sessions", json={"model": "qwen3.6:35b", "backend": "test"})
        assert prepared.status_code == 201
        body = prepared.json()
        assert body["session"]["client"] == "qwen"
        assert body["env"]["OPENAI_BASE_URL"].endswith(f"/session/{body['session']['session_id']}/v1")
        assert "$env:OPENAI_MODEL = 'qwen3.6:35b'" in body["shell"]["powershell"]
        assert client.post("/api/agents/missing/sessions", json={"model": "m"}).status_code == 404
        models = client.get("/api/models").json()["backends"]["test"]
        assert models["online"] and models["version"] == "0.35.1"
        assert models["models"][0]["loaded"] and models["models"][0]["context_length"] == 65536
    finally:
        close_app(app, client, old)


def test_anthropic_non_stream_translation(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["messages"][-1] == {"role": "user", "content": "Hi"}
        return httpx.Response(200, json={"message": {"content": "Hello"}, "prompt_eval_count": 3, "eval_count": 1})

    app, client, old = make_app(tmp_path, handler)
    try:
        session = client.post("/api/sessions", json={"client": "claude", "backend": "test"}).json()
        response = client.post(f"/session/{session['session_id']}/anthropic/v1/messages", json={"model": "model", "max_tokens": 20, "messages": [{"role": "user", "content": "Hi"}]})
        assert response.status_code == 200
        assert response.json()["content"][0]["text"] == "Hello"
        record = json.loads(client.get(f"/api/sessions/{session['session_id']}/telemetry").text)
        assert record["input_tokens"] == 3
        assert record["output_tokens"] == 1
    finally:
        close_app(app, client, old)


def test_unknown_backend_is_rejected(tmp_path):
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"client": "test", "backend": "missing"})
        assert response.status_code == 400


def test_request_history_includes_all_sessions_and_saved_traces(tmp_path):
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    app = create_app(settings)
    store = app.state.runtime.sessions
    traced = store.create({"client": "review", "backend": "test", "trace": False})
    other = store.create({"client": "other", "backend": "test", "trace": True})
    for index in range(105):
        store.append_request(traced, {"request_id": f"{index:06d}", "timestamp": f"2026-10-03T00:00:{index:03d}Z"})
    payload = {"request": {"messages": [{"role": "user", "content": "complete payload " * 1000}]}, "response": [{"message": {"content": "complete response"}}]}
    store.write_trace(traced, "000104", payload)
    store.append_request(other, {"request_id": "000001", "timestamp": "2026-10-04T00:00:00Z"})
    with (store.root / traced.session_id / "requests.jsonl").open("a") as handle:
        handle.write('invalid json\n[]\n')
    with TestClient(app) as client:
        records = client.get("/api/requests").json()
        assert len(records) == 106
        assert records[0]["session_id"] == other.session_id
        assert records[0]["trace_available"] is False
        filtered = client.get("/api/requests", params={"session_id": traced.session_id}).json()
        assert len(filtered) == 105
        assert filtered[0]["trace_available"] is True
        assert filtered[-1]["trace_available"] is False
        assert client.get(f"/api/sessions/{traced.session_id}/traces/000104").json() == payload
        assert client.get("/api/requests", params={"session_id": "missing"}).status_code == 404


def test_dashboard_trace_toggle_preserves_session_lifecycle(tmp_path):
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={"client": "review", "backend": "test"}).json()
        url = f"/api/sessions/{session['session_id']}"
        enabled = client.patch(url, json={"ended": False, "trace": True}).json()
        assert enabled["trace"] is True
        assert enabled["ended_at"] is None
        ended = client.patch(url, json={"ended": True, "exit_status": 0}).json()
        disabled = client.patch(url, json={"ended": False, "trace": False}).json()
        assert disabled["trace"] is False
        assert disabled["ended_at"] == ended["ended_at"]
        assert disabled["exit_status"] == 0


def test_dashboard_clears_data_and_deletes_only_selected_session(tmp_path):
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(200, json={"message": {"content": "Hello"}, "prompt_eval_count": 3, "eval_count": 1}))
    try:
        first = client.post("/api/sessions", json={"client": "first", "backend": "test", "trace": True}).json()
        second = client.post("/api/sessions", json={"client": "second", "backend": "test", "trace": True}).json()
        for session in (first, second):
            client.post(f"/session/{session['session_id']}/api/chat", json={"model": "test", "stream": False})
        url = f"/api/sessions/{first['session_id']}"
        cleared = client.delete(f"{url}/data")
        assert cleared.status_code == 200
        assert cleared.json()["request_count"] == 0
        assert cleared.json()["trace"] is True
        assert cleared.json()["ended_at"] is None
        assert client.get(f"{url}/telemetry").text == ""
        assert client.get(f"{url}/traces").json() == []
        status = client.get("/api/status").json()
        assert status["total_requests"] == 1
        assert status["total_input_tokens"] == 3
        assert status["total_output_tokens"] == 1
        assert all(record["session_id"] == second["session_id"] for record in status["recent_requests"])
        response = client.post(f"/session/{first['session_id']}/api/chat", json={"model": "test", "stream": False})
        assert response.headers["x-request-id"] == "000002"
        assert client.delete(url).status_code == 200
        assert not (tmp_path / "sessions" / first["session_id"]).exists()
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        assert client.get(f"/api/sessions/{second['session_id']}/traces/000001").status_code == 200
        assert len(client.get("/api/requests").json()) == 1
        assert client.get("/api/status").json()["total_requests"] == 1
        reloaded = create_app(app.state.runtime.settings)
        assert reloaded.state.runtime.sessions.get(first["session_id"]) is None
        assert reloaded.state.runtime.total_input_tokens == 3
    finally:
        close_app(app, client, old)


def test_dashboard_rejects_cleanup_during_non_streaming_requests(tmp_path):
    async def handler(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test") as api:
            for suffix in ("/data", ""):
                response = await api.delete(f"/api/sessions/{session['session_id']}{suffix}")
                assert response.status_code == 409
        return httpx.Response(200, json={"message": {"content": "Hello"}})

    app, client, old = make_app(tmp_path, handler)
    try:
        session = client.post("/api/sessions", json={"client": "busy", "backend": "test"}).json()
        response = client.post(f"/session/{session['session_id']}/api/chat", json={"model": "test", "stream": False})
        assert response.status_code == 200
        assert app.state.runtime.active == {}
        assert client.delete(f"/api/sessions/{session['session_id']}/data").status_code == 200
    finally:
        close_app(app, client, old)


def test_dashboard_updates_and_persists_network_config(tmp_path):
    config_path = tmp_path / "backends.yaml"
    config_path.write_text("extra_setting: preserved\n", encoding="utf-8")
    settings = Settings(
        log_dir=tmp_path / "logs",
        backends_file=config_path,
        default_backend="test",
        backends={"test": Backend("test", "http://ollama.test")},
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        response = client.put("/api/config", json={
            "proxy": {
                "listen_host": "0.0.0.0",
                "listen_port": 9191,
                "url": "http://10.0.0.5:9191/",
            },
            "default_backend": "desktop",
            "backends": {
                "laptop": {"type": "ollama", "url": "http://127.0.0.1:11434"},
                "desktop": {"type": "ollama", "url": "http://10.0.0.6:11434/"},
            },
        })
        assert response.status_code == 200
        assert response.json()["restart_required"] is True
        current = client.get("/api/config").json()["config"]
        assert current["default_backend"] == "desktop"
        assert current["proxy"]["url"] == "http://10.0.0.5:9191"
        assert current["backends"]["desktop"]["url"] == "http://10.0.0.6:11434"
        assert client.post("/api/sessions", json={"client": "test", "backend": "desktop"}).status_code == 201

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["extra_setting"] == "preserved"
    assert saved["proxy"]["listen_port"] == 9191


def test_dashboard_rejects_invalid_network_config(tmp_path):
    settings = Settings(
        log_dir=tmp_path / "logs",
        backends_file=tmp_path / "backends.yaml",
        default_backend="test",
        backends={"test": Backend("test", "http://ollama.test")},
    )
    with TestClient(create_app(settings)) as client:
        response = client.put("/api/config", json={
            "proxy": {"listen_host": "127.0.0.1", "listen_port": 70000, "url": "bad"},
            "default_backend": "missing",
            "backends": {"test": {"type": "ollama", "url": "http://ollama.test"}},
        })
        assert response.status_code == 400


def test_live_hub_streams_reasoning_and_content_while_request_runs(tmp_path):
    newline = chr(10)

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            {"message": {"thinking": "Let me think"}, "done": False},
            {"message": {"content": "Answer"}, "done": False},
            {"message": {"content": ""}, "done": True, "eval_count": 2},
        ]
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content="".join(json.dumps(line) + newline for line in lines).encode())

    app, client, old = make_app(tmp_path, handler)
    try:
        hub = app.state.runtime.live
        queue = hub.subscribe()
        session = client.post("/api/sessions", json={"client": "qwen", "backend": "test"}).json()
        with client.stream("POST", f"/session/{session['session_id']}/v1/chat/completions", json={"model": "m", "stream": True, "messages": []}) as response:
            response.read()
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert [event["type"] for event in events] == ["start", "summary", "delta", "delta", "end"]
        assert events[1]["summary"]["translated"] is True
        assert (events[2]["kind"], events[2]["text"]) == ("reasoning", "Let me think")
        assert (events[3]["kind"], events[3]["text"]) == ("content", "Answer")
        details = client.get(f"/api/live/{session['session_id']}/000001").json()
        assert details["request"] == {"model": "m", "stream": True, "messages": []}
        assert details["upstream"]["messages"] == []
        assert details["raw_total"] == 3 and details["raw"][0]["message"]["thinking"] == "Let me think"
        assert client.get(f"/api/live/{session['session_id']}/000099").status_code == 404
        snapshot = hub.snapshot()
        assert snapshot["active"] == []
        assert snapshot["recent"][0]["reasoning"] == "Let me think"
        assert snapshot["recent"][0]["record"]["output_tokens"] == 2
        client.delete(f"/api/sessions/{session['session_id']}")
        assert hub.snapshot()["recent"] == []
    finally:
        close_app(app, client, old)


def test_hosts_report_ollama_ps_remote_metrics_and_traffic(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:35b", "size": 200, "size_vram": 50, "context_length": 65536, "expires_at": "2026-10-03T18:00:00Z"}]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.35.1"})
        if request.url.host == "metrics.test":
            return httpx.Response(200, json={"cpu": {"percent": 12.5}, "memory": {"used": 1, "total": 2, "percent": 50}, "gpus": []})
        return httpx.Response(404)

    settings_backends = {
        "test": Backend("test", "http://ollama.test"),
        "desk": Backend("desk", "http://desk.test", metrics_url="http://metrics.test:8182"),
    }
    settings = Settings(log_dir=tmp_path, default_backend="test", backends=settings_backends)
    app = create_app(settings)
    with TestClient(app) as client:
        real = app.state.runtime.client
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            hosts = {name: client.get(f"/api/hosts/{name}").json() for name in ("test", "desk")}
            assert client.get("/api/hosts/missing").status_code == 404
        finally:
            import asyncio

            asyncio.run(app.state.runtime.client.aclose())
            app.state.runtime.client = real
    assert hosts["test"]["online"] and hosts["test"]["version"] == "0.35.1"
    assert hosts["test"]["models"][0]["gpu_percent"] == 25
    assert hosts["test"]["machine"]["source"] is None and "agent-metrics" in hosts["test"]["machine"]["error"]
    assert hosts["desk"]["machine"] == {"source": "remote", "data": {"cpu": {"percent": 12.5}, "memory": {"used": 1, "total": 2, "percent": 50}, "gpus": []}}
    assert hosts["desk"]["traffic"]["in_flight"] == 0
    assert hosts["desk"]["name"] == "desk" and hosts["desk"]["sampled_at"] > 0


def test_metrics_url_survives_config_round_trip(tmp_path):
    settings = Settings(log_dir=tmp_path, backends_file=tmp_path / "backends.yaml", default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    with TestClient(create_app(settings)) as client:
        body = client.get("/api/config").json()["config"]
        body["backends"]["test"]["metrics_url"] = "http://10.0.0.6:8182/"
        saved = client.put("/api/config", json=body)
        assert saved.status_code == 200
        assert saved.json()["config"]["backends"]["test"]["metrics_url"] == "http://10.0.0.6:8182"
        body["backends"]["test"]["metrics_url"] = "not a url"
        assert client.put("/api/config", json=body).status_code == 400


def test_sessions_record_last_activity_and_backfill_from_history(tmp_path):
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(200, json={"message": {"content": "Hi"}}))
    try:
        session = client.post("/api/sessions", json={"client": "qwen", "backend": "test"}).json()
        assert session["last_activity_at"] is None
        client.post(f"/session/{session['session_id']}/api/chat", json={"model": "m", "stream": False})
        updated = client.get(f"/api/sessions/{session['session_id']}").json()
        assert updated["last_activity_at"] is not None
        assert updated["last_activity_at"] >= session["started_at"]
    finally:
        close_app(app, client, old)

    # A session saved before activity tracking takes its time from the newest request record.
    store_dir = tmp_path / "sessions" / session["session_id"]
    data = json.loads((store_dir / "session.json").read_text())
    data.pop("last_activity_at")
    (store_dir / "session.json").write_text(json.dumps(data))
    record = json.loads((store_dir / "requests.jsonl").read_text().splitlines()[-1])
    reloaded = create_app(app.state.runtime.settings).state.runtime.sessions.get(session["session_id"])
    assert reloaded.last_activity_at == record["timestamp"]


def test_live_snapshot_filters_by_session(tmp_path):
    from agent_proxy.live import LiveHub
    from agent_proxy.telemetry import RequestTelemetry

    hub = LiveHub()
    for session_id in ("one", "two"):
        hub.start(f"{session_id}:000001", RequestTelemetry(session_id, "000001", "qwen", "m", "b", "/v1", 0))
    assert [entry["session_id"] for entry in hub.snapshot("two")["active"]] == ["two"]
    assert len(hub.snapshot()["active"]) == 2
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/live", params={"session_id": "missing"}).status_code == 404


def test_live_view_receives_non_streamed_replies(tmp_path):
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(200, json={"message": {"content": "Hi there"}, "eval_count": 2}))
    try:
        session = client.post("/api/sessions", json={"client": "aider", "backend": "test"}).json()
        client.post(f"/session/{session['session_id']}/api/chat", json={"model": "m", "stream": False})
        recent = app.state.runtime.live.snapshot(session["session_id"])["recent"]
        assert recent[0]["content"] == "Hi there"
    finally:
        close_app(app, client, old)
