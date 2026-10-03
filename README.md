# Local AI Agent Framework

A lightweight, streaming-first framework for running Codex CLI and Claude Code against local or remote Ollama instances. Every model request follows the same path through an OpenAI- and Anthropic-compatible proxy, so agent runs and benchmarks share routing, logs, traces, and performance measurements.

## Install

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Manage all proxy and Ollama locations in `config/backends.yaml`:

```yaml
proxy:
  listen_host: 127.0.0.1
  listen_port: 8181
  url: http://127.0.0.1:8181

default_backend: laptop

backends:
  laptop:
    type: ollama
    url: http://localhost:11434
  desktop:
    type: ollama
    url: http://192.168.1.133:11434
```

`listen_host` and `listen_port` control where the proxy process binds. `url` is the address launchers and benchmarks use to reach it. For a proxy shared over your network, use `listen_host: 0.0.0.0` and set `url` to that computer's LAN address, such as `http://192.168.1.50:8181`.

## Run the proxy

```bash
local-agent-proxy
# or: python -m local_agent.proxy.app
```

The proxy uses the configured address and its dashboard displays active work and complete saved request history. Configuration can be overridden with `AI_PROXY_HOST`, `AI_PROXY_PORT`, `AI_PROXY_URL`, `AI_PROXY_LOG_DIR`, `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS`, and `AI_PROXY_DEFAULT_BACKEND`.

### Review requests and responses

Open **Requests** to search the saved history or filter by session, captured traces, or errors. Select a request to inspect its timing, token counts, and status. History is paginated without the previous 100-request limit.

For requests with a captured trace, the inspector has **Request**, **Response**, **Conversation**, and **Metadata** tabs. The JSON tabs display the complete captured payloads, including all saved streaming events, without truncation. Use wrapping, copying, individual payload downloads, or **Full trace** to export the entire capture. Conversation is a readable view; the JSON tabs remain the authoritative captured data.

Enable **Full tracing** when creating a session, toggle it on the **Sessions** tab, or use **Enable tracing** in an untraced request's inspector. This affects future requests only; historical payloads cannot be recovered. Existing captures remain viewable after tracing is disabled. Captures store parsed JSON, not byte-for-byte HTTP headers or stream framing.

Trace payloads may contain source code or secrets. The dashboard has no authentication; keep the proxy bound to localhost or restrict access to a trusted network.

On **Sessions**, **Clear data** permanently removes one session's saved request telemetry and traces while keeping its settings. **Delete session** removes the session and all its saved data. Both require confirmation and are blocked while the session has requests in flight. Clearing data resets its displayed request count but does not reuse request IDs. Proxy logs and network configuration are not removed.

### Configure from the dashboard

Open **Settings** in the dashboard and use **Network configuration** to:

- change the proxy listen host, port, and client URL;
- choose the default backend;
- edit the laptop or desktop Ollama address;
- add or remove backend profiles.

Saving writes the changes to `config/backends.yaml`. Backend and default-selection changes apply immediately. A listen host or port change displays a restart notice because the running server must rebind its network socket.

Useful endpoints:

- `POST /api/sessions` creates a session.
- `PATCH /api/sessions/{id}` closes it or changes tracing.
- `DELETE /api/sessions/{id}/data` clears its saved telemetry and traces.
- `DELETE /api/sessions/{id}` deletes the session and its saved data.
- `/session/{id}/v1/` is the session-scoped OpenAI API.
- `/session/{id}/anthropic/` is the session-scoped Anthropic API.
- `/session/{id}/api/` is an optional native Ollama pass-through.
- `GET /api/status` returns dashboard state.
- `GET /api/requests` returns complete saved request metadata and trace availability; optionally filter with `?session_id={id}`.
- `GET /api/config` and `PUT /api/config` power the dashboard configuration editor.

Requests made to unscoped compatibility URLs get an implicit session. A caller may instead send `X-AI-Proxy-Session`.

## Launch an agent

The launcher starts the proxy when necessary, creates a session, configures the agent, waits for it, and records its exit status.

```bash
agent codex . --model qwen3.6 --trace
agent claude . --model qwen3.6 --backend desktop

./launcher/agent.sh codex . --model qwen3.6
./launcher/agent.ps1 codex . --model qwen3.6
```

Arguments after `--` are passed to the underlying agent.

Without `--proxy` or `--backend`, the launcher uses `proxy.url` and `default_backend` from `config/backends.yaml`. The benchmark command also uses `proxy.url` from this file. Command-line flags are temporary overrides only.

## Run benchmarks

Start the proxy, then run the included example or add a directory beneath `benchmarks/tests` containing `prompt.md` and `test.yaml`.

```bash
local-agent-benchmark reasoning
local-agent-benchmark --all
local-agent-benchmark reasoning --model qwen3.6 --backend desktop
```

Every run has a separate proxy session. Machine-readable and Markdown output is written beneath `benchmarks/results/<timestamp>`.

## Logs and traces

```text
logs/
├── proxy/proxy.log
└── sessions/<session-id>/
    ├── session.json
    ├── requests.jsonl
    └── traces/000001.json
```

Tracing is off by default because full payloads can contain source code and secrets. Ollama-provided token counts and durations are retained as authoritative telemetry.
