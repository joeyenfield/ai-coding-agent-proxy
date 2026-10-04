from __future__ import annotations

import json

from agent_proxy.agents import load_agents
from agent_proxy.benchmark import summarize
from agent_proxy.config import ROOT, Settings
from agent_proxy.telemetry import RequestTelemetry
from agent_proxy.translate import (
    OpenAIStreamTranslator,
    anthropic_to_ollama,
    ollama_to_anthropic,
    ollama_to_openai,
    openai_to_ollama,
)


def test_telemetry_uses_ollama_counts_and_durations():
    item = RequestTelemetry("session", "000001", "test", "model", "backend", "/api/chat", 10)
    item.apply_ollama_stats({"prompt_eval_count": 20, "prompt_eval_duration": 2_000_000_000, "eval_count": 30, "eval_duration": 3_000_000_000})
    record = item.finish()
    assert record["prompt_tps"] == 10
    assert record["generation_tps"] == 10


def test_anthropic_translation_handles_system_blocks():
    settings = Settings(models={"alias": {"ollama_model": "real"}})
    result = anthropic_to_ollama({"model": "alias", "system": [{"type": "text", "text": "Rules"}], "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}], "max_tokens": 5}, settings.profile("alias"))
    assert result["model"] == "real"
    assert result["messages"] == [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Hello"}]


def test_anthropic_translation_preserves_tool_calls_and_results():
    settings = Settings()
    result = anthropic_to_ollama({"model": "model", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "one", "name": "read", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "one", "content": "data"}]},
    ]}, settings.profile("model"))
    assert result["messages"][0]["tool_calls"][0]["function"]["name"] == "read"
    assert result["messages"][1] == {"role": "tool", "content": "data", "tool_name": "read"}
    response = ollama_to_anthropic({"message": {"content": "", "tool_calls": [{"function": {"name": "write", "arguments": {"path": "y"}}}]}}, "model")
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["name"] == "write"


def test_benchmark_summary_ignores_missing_values():
    data = [{"error": None, "metrics": {"ttft_ms": 2}}, {"error": "bad", "metrics": {"ttft_ms": 4}}]
    result = summarize(data)
    assert result["successful"] == 1
    assert result["ttft_ms"] == {"average": 3, "minimum": 2, "maximum": 4}


def test_network_locations_load_from_central_config(tmp_path, monkeypatch):
    config = tmp_path / "backends.yaml"
    config.write_text(
        """proxy:
  listen_host: 0.0.0.0
  listen_port: 9191
  url: http://10.0.0.5:9191
default_backend: desktop
backends:
  desktop:
    type: ollama
    url: http://10.0.0.6:11434
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PROXY_BACKENDS", str(config))
    monkeypatch.delenv("AI_PROXY_HOST", raising=False)
    monkeypatch.delenv("AI_PROXY_PORT", raising=False)
    monkeypatch.delenv("AI_PROXY_URL", raising=False)
    settings = Settings.load()
    assert settings.host == "0.0.0.0"
    assert settings.port == 9191
    assert settings.proxy_url == "http://10.0.0.5:9191"
    assert settings.default_backend == "desktop"
    assert settings.backend(None).url == "http://10.0.0.6:11434"


QWEN_SETTINGS = dict(
    model_defaults={"keep_alive": "30m", "options": {"num_ctx": 65536}},
    model_profiles=[
        {"name": "qwen-coder", "match": ["qwen3*coder*", "qwen3*coding*"], "options": {"temperature": 0.7}},
        {"name": "qwen3", "match": "qwen3*", "options": {"temperature": 0.6, "top_k": 20}},
    ],
    models={"fast": {"ollama_model": "qwen3.6:35b", "think": False, "options": {"temperature": 0.8}}},
)


def test_profiles_layer_defaults_first_matching_pattern_and_alias():
    settings = Settings(**QWEN_SETTINGS)
    coder = settings.profile("qwen3.6:27b-coding")
    assert coder.options == {"num_ctx": 65536, "temperature": 0.7}
    assert coder.matched == ["defaults", "qwen-coder"]
    alias = settings.profile("fast")
    assert alias.ollama_model == "qwen3.6:35b"
    assert alias.options == {"num_ctx": 65536, "temperature": 0.8, "top_k": 20}
    assert alias.think is False and alias.keep_alive == "30m"
    assert settings.profile("gemma4:e4b").options == {"num_ctx": 65536}


def test_openai_request_translates_to_native_ollama_with_profile():
    settings = Settings(**QWEN_SETTINGS)
    payload = {
        "model": "fast", "stream": True, "max_tokens": 100, "temperature": 0.1,
        "messages": [
            {"role": "developer", "content": [{"type": "text", "text": "Be brief"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "file"},
        ],
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
    }
    result = openai_to_ollama(payload, settings.profile("fast"))
    assert result["model"] == "qwen3.6:35b"
    assert result["options"] == {"num_ctx": 65536, "temperature": 0.1, "top_k": 20, "num_predict": 100}
    assert result["think"] is False and result["keep_alive"] == "30m"
    assert result["messages"][0] == {"role": "system", "content": "Be brief"}
    assert result["messages"][1]["tool_calls"] == [{"function": {"name": "read", "arguments": {"path": "a"}}}]
    assert result["messages"][2] == {"role": "tool", "content": "file", "tool_name": "read"}
    assert result["tools"] == payload["tools"]


def test_openai_stream_translator_emits_reasoning_tools_and_usage():
    translator = OpenAIStreamTranslator("qwen", include_usage=True)
    events = translator.feed({"message": {"thinking": "hmm"}, "done": False})
    events += translator.feed({"message": {"tool_calls": [{"function": {"name": "read", "arguments": {"path": "a"}}}]}, "done": False})
    events += translator.feed({"message": {"content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 3})
    chunks = [json.loads(event[6:]) for event in events if event != b"data: [DONE]\n\n"]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "reasoning_content": "hmm"}
    call = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0 and call["function"] == {"name": "read", "arguments": '{"path":"a"}'}
    assert chunks[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[3]["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert events[-1] == b"data: [DONE]\n\n"
    assert translator.finish() == []


def test_openai_non_stream_response_shape():
    response = ollama_to_openai({"message": {"content": "hi", "thinking": "t"}, "done_reason": "length", "prompt_eval_count": 2, "eval_count": 1}, "m")
    choice = response["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "hi", "reasoning_content": "t"}
    assert choice["finish_reason"] == "length"
    assert response["usage"]["total_tokens"] == 3


def test_bundled_agents_render_session_endpoints():
    agents = load_agents(ROOT / "config" / "agents.yaml")
    assert {"qwen", "opencode", "copilot", "claude", "codex"} <= set(agents)
    qwen = agents["qwen"].render("http://proxy:8181", "abc", "qwen3.6:35b")
    assert qwen["env"]["OPENAI_BASE_URL"] == "http://proxy:8181/session/abc/v1"
    assert qwen["command"] == ["qwen", "--auth-type", "openai", "--model", "qwen3.6:35b"]
    copilot = agents["copilot"].render("http://proxy:8181", "abc", "m")
    assert copilot["env"]["COPILOT_PROVIDER_BASE_URL"] == "http://proxy:8181/session/abc/v1"
    opencode = json.loads(agents["opencode"].render("http://proxy:8181", "abc", "m")["env"]["OPENCODE_CONFIG_CONTENT"])
    assert opencode["provider"]["agent-proxy"]["options"]["baseURL"] == "http://proxy:8181/session/abc/v1"
    assert "m" in opencode["provider"]["agent-proxy"]["models"]


def test_launcher_runs_agent_with_rendered_environment(tmp_path, monkeypatch):
    import subprocess
    import sys

    from agent_proxy import launcher
    from agent_proxy.agents import AgentDefinition

    calls = {}

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {"session_id": "s1"}

    class FakeClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, url, json):
            calls["session"] = json
            return FakeResponse()

    def fake_run(command, cwd, env, check):
        calls.update(command=command, env=env)
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(launcher, "ensure_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher.httpx, "Client", FakeClient)
    def fake_patch(url, json, timeout):
        calls["closed"] = json
        return FakeResponse()

    monkeypatch.setattr(launcher.httpx, "patch", fake_patch)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    agent = AgentDefinition(
        id="fake", name="Fake", executable=sys.executable, protocol="openai-chat",
        env={"OPENAI_BASE_URL": "{openai_url}"}, args=["--model", "{model}"],
    )
    status = launcher.launch("fake", tmp_path, "qwen3.6:35b", "laptop", True, "http://proxy", ["--yolo"], agents={"fake": agent})
    assert status == 3
    assert calls["session"]["client"] == "fake" and calls["session"]["trace"] is True
    assert calls["command"][1:] == ["--model", "qwen3.6:35b", "--yolo"]
    assert calls["env"]["OPENAI_BASE_URL"] == "http://proxy/session/s1/v1"
    assert calls["closed"] == {"ended": True, "exit_status": 3}


def test_stream_deltas_cover_every_stream_format():
    from agent_proxy.telemetry import stream_deltas

    assert stream_deltas({"message": {"thinking": "hm", "content": "hi"}}) == [("reasoning", "hm"), ("content", "hi")]
    assert stream_deltas({"response": "x", "thinking": "t"}) == [("reasoning", "t"), ("content", "x")]
    assert stream_deltas({"choices": [{"delta": {"reasoning": "r", "content": "c"}}]}) == [("reasoning", "r"), ("content", "c")]
    assert stream_deltas({"choices": [{"delta": {"tool_calls": [{"function": {"name": "read", "arguments": "{"}}]}}]}) == [("tool", "read({")]
    assert stream_deltas({"type": "response.reasoning_text.delta", "delta": "why"}) == [("reasoning", "why")]
    assert stream_deltas({"type": "response.completed"}) == []


def test_stream_deltas_read_complete_responses():
    from agent_proxy.telemetry import stream_deltas

    assert stream_deltas({"choices": [{"message": {"content": "done", "reasoning_content": "why"}}]}) == [("reasoning", "why"), ("content", "done")]
    anthropic = {"type": "message", "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Read", "input": {"p": 1}}]}
    assert stream_deltas(anthropic) == [("content", "hi"), ("tool", 'Read({"p": 1})' + chr(10))]


def test_cpu_reading_reuses_value_for_back_to_back_calls(monkeypatch):
    from agent_proxy import host_metrics

    readings = iter([40.0, 0.0])
    monkeypatch.setattr(host_metrics.psutil, "cpu_percent", lambda interval=None: next(readings))
    monkeypatch.setattr(host_metrics, "_cpu_primed", False)
    assert host_metrics._cpu_percent() == 40.0
    assert host_metrics._cpu_percent() == 40.0  # too soon to measure again


def test_cut_off_stream_estimates_tokens_and_is_flagged():
    item = RequestTelemetry("s", "000001", "claude", "m", "b", "/anthropic/v1/messages", 10, estimated_input_tokens=900)
    item.streaming = True
    item.streamed_chunks = 2703
    record = item.finish()
    assert record["output_tokens"] == 2703 and record["input_tokens"] == 900
    assert record["tokens_estimated"] is True
    assert record["error_type"] == "incomplete_stream"


def test_completed_stream_keeps_reported_counts():
    item = RequestTelemetry("s", "000001", "qwen", "m", "b", "/v1/chat/completions", 10, estimated_input_tokens=900)
    item.streaming = True
    item.streamed_chunks = 50
    item.apply_ollama_stats({"done": True, "prompt_eval_count": 120, "eval_count": 48})
    record = item.finish()
    assert (record["input_tokens"], record["output_tokens"], record["tokens_estimated"], record["error_type"]) == (120, 48, False, None)


def test_anthropic_stream_forwards_reasoning_as_thinking_blocks():
    from agent_proxy.translate import AnthropicStreamTranslator

    translator = AnthropicStreamTranslator("qwen")
    events = translator.start()
    events += translator.feed({"message": {"thinking": "Let me "}})
    events += translator.feed({"message": {"thinking": "think"}})
    events += translator.feed({"message": {"content": "Answer"}})
    events += translator.feed({"message": {"content": ""}, "done": True, "eval_count": 3})
    events += translator.finish()
    parsed = [json.loads(event.decode().split("data: ", 1)[1]) for event in events]
    starts = [(event["index"], event["content_block"]["type"]) for event in parsed if event["type"] == "content_block_start"]
    assert starts == [(0, "thinking"), (1, "text")]
    deltas = [event["delta"] for event in parsed if event["type"] == "content_block_delta"]
    assert deltas[0] == {"type": "thinking_delta", "thinking": "Let me "}
    assert deltas[2]["type"] == "signature_delta"
    assert deltas[3] == {"type": "text_delta", "text": "Answer"}
    assert [event["index"] for event in parsed if event["type"] == "content_block_stop"] == [0, 1]
    assert translator.ping().startswith(b"event: ping")


async def test_keepalive_ticks_during_silence_without_losing_lines():
    import asyncio

    from agent_proxy.routes.compat import _keepalive

    async def slow():
        await asyncio.sleep(0.25)
        yield "first"
        yield "second"

    items = [item async for item in _keepalive(slow(), interval=0.1)]
    assert items.count(None) >= 1
    assert [item for item in items if item is not None] == ["first", "second"]


def test_live_summary_describes_agent_request_and_ollama_settings():
    from agent_proxy.live import summarize

    request = {
        "model": "fast", "stream": True, "system": "You are Claude Code.",
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
        "tools": [{"name": "Read"}, {"type": "function", "function": {"name": "grep"}}],
        "max_tokens": 32000,
    }
    upstream = {"model": "qwen3.6:35b", "think": False, "keep_alive": "30m", "options": {"num_ctx": 65536, "temperature": 0.7, "num_predict": 32000}}
    summary = summarize(request, upstream)
    assert summary["messages"] == 2 and summary["tools"] == 2 and summary["tool_names"] == ["Read", "grep"]
    assert summary["system_chars"] == len("You are Claude Code.")
    assert (summary["ollama_model"], summary["num_ctx"], summary["temperature"], summary["think"]) == ("qwen3.6:35b", 65536, 0.7, False)
    assert summary["translated"] is True


def test_usage_parsing_ignores_non_model_json():
    from agent_proxy.telemetry import RequestTelemetry

    telemetry = RequestTelemetry("s", "1", "c", "", "b", "/", 0)
    telemetry.apply_ollama_stats({"message": "Not Found", "response": "x", "usage": "n/a"})
    assert telemetry.input_tokens == 0
    telemetry.apply_ollama_stats({"usage": {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}})
    assert (telemetry.input_tokens, telemetry.cache_read_tokens) == (5, 3)


def test_ui_build_detects_stale_sources_and_skips_without_sources(tmp_path, monkeypatch):
    import os

    from agent_proxy import ui_build

    dist = tmp_path / "ui" / "dist"
    assert ui_build.ensure_built(dist) is False  # no package.json: nothing to build, nothing built
    (tmp_path / "ui" / "src").mkdir(parents=True)
    (tmp_path / "ui" / "package.json").write_text("{}")
    source = tmp_path / "ui" / "src" / "App.tsx"
    source.write_text("x")
    assert ui_build.stale(dist)
    dist.mkdir()
    (dist / "index.html").write_text("<html>")
    os.utime(source, (1, 1))
    os.utime(tmp_path / "ui" / "package.json", (1, 1))
    assert not ui_build.stale(dist)
    source.write_text("changed")
    assert ui_build.stale(dist)

    calls = []
    monkeypatch.setattr(ui_build.shutil, "which", lambda name: "npm")
    monkeypatch.setattr(ui_build.subprocess, "run", lambda command, **_: calls.append(command[1:]) or type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})())
    # A failed build falls back to the existing dist.
    assert ui_build.ensure_built(dist) is True
    assert calls == [["install", "--no-audit", "--no-fund"]]


def test_backend_for_picks_a_backend_of_the_agents_type():
    import pytest

    from agent_proxy.config import Backend

    settings = Settings(default_backend="laptop", backends={
        "laptop": Backend("laptop", "http://l"), "anthropic": Backend("anthropic", "https://a", type="anthropic"),
    })
    assert settings.backend_for("ollama").name == "laptop"
    assert settings.backend_for("anthropic").name == "anthropic"
    with pytest.raises(ValueError, match="needs a backend of type anthropic"):
        settings.backend_for("anthropic", "laptop")
    agents = load_agents(ROOT / "config" / "agents.yaml")
    assert agents["claude-subscription"].route == "hosted"
    assert agents["claude-account"].route == "account"
    assert agents["qwen"].route == "ollama"
