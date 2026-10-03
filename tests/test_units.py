from __future__ import annotations

from local_agent.benchmark import summarize
from local_agent.proxy.app import _anthropic_to_ollama, _ollama_to_anthropic
from local_agent.proxy.telemetry import RequestTelemetry
from local_agent.config import Settings


def test_telemetry_uses_ollama_counts_and_durations():
    item = RequestTelemetry("session", "000001", "test", "model", "backend", "/api/chat", 10)
    item.apply_ollama_stats({"prompt_eval_count": 20, "prompt_eval_duration": 2_000_000_000, "eval_count": 30, "eval_duration": 3_000_000_000})
    record = item.finish()
    assert record["prompt_tps"] == 10
    assert record["generation_tps"] == 10


def test_anthropic_translation_handles_system_blocks():
    settings = Settings(models={"alias": {"ollama_model": "real"}})
    result = _anthropic_to_ollama({"model": "alias", "system": [{"type": "text", "text": "Rules"}], "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}], "max_tokens": 5}, settings)
    assert result["model"] == "real"
    assert result["messages"] == [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Hello"}]


def test_anthropic_translation_preserves_tool_calls_and_results():
    settings = Settings()
    result = _anthropic_to_ollama({"model": "model", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "one", "name": "read", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "one", "content": "data"}]},
    ]}, settings)
    assert result["messages"][0]["tool_calls"][0]["function"]["name"] == "read"
    assert result["messages"][1] == {"role": "tool", "content": "data"}
    response = _ollama_to_anthropic({"message": {"content": "", "tool_calls": [{"function": {"name": "write", "arguments": {"path": "y"}}}]}}, "model")
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
