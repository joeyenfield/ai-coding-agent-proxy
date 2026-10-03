# AI Coding Agent Proxy

Run terminal coding agents such as Qwen Code, OpenCode, GitHub Copilot CLI, Claude Code, Codex, Aider and Goose against your own Ollama machines. Every model request flows through one streaming proxy, which records timing, tokens and payloads. The proxy also applies tuned Ollama settings, so local Qwen models get the context window and sampling they need.

![Overview: live routing from agents through the proxy to two Ollama backends](docs/screenshots/overview.png)

## What's in the repository

```text
proxy/      Python service: FastAPI proxy, management API, agent launcher, benchmarks
ui/         Web console: React + TypeScript + Vite
config/     backends.yaml (network), models.yaml (Ollama tuning), agents.yaml (agent definitions)
benchmarks/ benchmark tests and results
scripts/    launcher shims, dev runner, Ollama start script, screenshot tooling
```

```text
 Qwen Code ─┐                         ┌─ laptop   (localhost:11434)
 OpenCode  ─┤   OpenAI / Anthropic /  │
 Copilot   ─┼─▶ native Ollama APIs ──▶┤
 Claude    ─┤      agent-proxy         │
 Codex ... ─┘   + UI on :8181          └─ desktop  (192.168.1.133:11434)
```

The proxy and the UI are separate projects. In production the proxy serves the built UI from `ui/dist` on the same port. In development, Vite serves the UI with hot reload and forwards `/api` to the proxy.

## Quick start

You need Python 3.10+, Node 20+ and at least one Ollama server.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e "./proxy[dev]"

cd ui && npm install && npm run build && cd ..

agent-proxy                     # http://127.0.0.1:8181
```

Then start an agent from your project folder:

```bash
agent --list                                     # configured agents and whether they're installed
agent qwen . --model qwen3.6:35b                 # Qwen Code on the default backend
agent opencode . --model qwen3.8:latest --backend desktop --trace
agent copilot . --model qwen3.6:27b-coding
agent claude . --model qwen3.6:35b -- --continue # arguments after -- go to the agent
```

The launcher starts the proxy if it isn't running and opens a session. It sets the agent's environment, waits for the agent to exit, then records the exit status.

## Supported agents

| Agent | Command | Protocol through the proxy | Install |
|---|---|---|---|
| Qwen Code | `agent qwen` | OpenAI Chat, translated to native Ollama | `npm i -g @qwen-code/qwen-code` |
| OpenCode | `agent opencode` | OpenAI Chat, translated to native Ollama | `npm i -g opencode-ai` |
| GitHub Copilot CLI | `agent copilot` | OpenAI Chat (BYOK, offline mode) | `npm i -g @github/copilot` |
| Claude Code | `agent claude` | Anthropic Messages, translated to native Ollama | `npm i -g @anthropic-ai/claude-code` |
| OpenAI Codex CLI | `agent codex` | OpenAI Responses, passed through | `npm i -g @openai/codex` |
| Aider | `agent aider` | Native Ollama | `aider-install` |
| Goose | `agent goose` | Native Ollama | see the Goose docs |

Agents are defined in [`config/agents.yaml`](config/agents.yaml) as environment variables and arguments with placeholders such as `{openai_url}` and `{model}`. To add another agent, add an entry there; no code changes are needed. The **Agents** page shows each one's install state and builds the launch command. It can also open a session and print the environment for running an agent by hand in PowerShell or bash.

![Agents page with the Qwen Code launch panel](docs/screenshots/agents.png)

### Which agent works best with Qwen on Ollama

1. **Qwen Code** is the first choice for Qwen 3.x. It's built by the Qwen team around the same tool-calling format the models are trained on, so tool calls parse reliably.
2. **OpenCode** is a close second. It has good local-provider support and a smaller system prompt than Claude Code, which matters with 27–35B models.
3. **Copilot CLI** and **Claude Code** work through the proxy. Claude Code's very large system prompt and tool list need a 64K+ context and are slower to process on local GPUs.

This ranking is a recommendation, not a measured benchmark. Use `agent-benchmark` and the Requests page to compare agents on your own hardware.

## Tuning Ollama for Qwen

Ollama's OpenAI-compatible `/v1` endpoint ignores per-request settings such as `num_ctx`. Agent conversations then get silently truncated to the server's default context. To avoid that, the proxy translates OpenAI Chat and Anthropic requests to Ollama's native `/api/chat`. It applies the matching profile from [`config/models.yaml`](config/models.yaml) and translates the reply back, including streaming, tool calls and reasoning.

```yaml
defaults:
  keep_alive: 30m          # keep models warm between agent turns
  options:
    num_ctx: 65536         # context for every model

profiles:                  # first match wins, matched against the Ollama tag
  - name: qwen-coder
    match: ["qwen3*coder*", "qwen3*coding*"]
    options: {temperature: 0.7, top_p: 0.8, top_k: 20, repeat_penalty: 1.05}
  - name: qwen3
    match: ["qwen3*"]      # qwen3, qwen3.5, qwen3.6, qwen3.8 ...
    options: {temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0}

models:                    # aliases agents can use
  qwen-fast:
    ollama_model: qwen3.6:35b
    think: false           # skip reasoning for quick edit loops
```

Values a client sends explicitly always override profile defaults. The **Models** page lists every model on each backend with the settings the proxy will apply. It also shows how much of each loaded model fits in VRAM.

![Models page showing proxy settings and GPU share](docs/screenshots/models.png)

If a model shows **less than 100% on GPU**, part of it runs on the CPU and generation slows down a lot. Fix it by any of:

- lowering `num_ctx` in `defaults` or in a profile;
- picking a smaller tag;
- starting Ollama with a quantized KV cache: `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0` (see [`scripts/start-ollama.sh`](scripts/start-ollama.sh)).

Codex uses the Responses API, which is passed through unchanged. Set `OLLAMA_CONTEXT_LENGTH` on the server for it.

## Watching requests live

The **Live** page streams every in-flight request token by token, with reasoning, the reply and tool calls in separate panes. It shows the current phase (waiting, thinking, writing or calling tools), time to first token, a running token count and live tokens per second. Finished requests stay on the page for a while so you can read the end of the output.

![Live page with Qwen reasoning streaming on the laptop](docs/screenshots/live.png)

Live text is held in memory only and never written to disk; saved payloads still follow each session's capture setting. Set `AI_PROXY_LIVE=0` to stream token counts without the text.

### One session at a time

The **Sessions** list shows when traffic last passed through each session, most recent first. Select a session to open its page, which streams that session's requests live, the same way as the Live page but for that agent run only. The page also shows the session's totals, its request history and its endpoint URLs.

![A session page streaming Qwen reasoning, with its request history below](docs/screenshots/session.png)

## Watching the Ollama machines

The **Hosts** page shows what each backend has loaded, like `ollama ps`: size, the GPU/CPU split, context length and when the model unloads. It also shows in-flight requests and recent speed for that backend, and charts CPU, memory, GPU load and GPU memory over the last five minutes, each on a fixed scale from 0 to 100% or to the machine's total memory. Hosts start collapsed; only the ones you expand are contacted, every two seconds, and collapsing one stops it. Samples are not saved.

![Hosts page with live CPU, memory and GPU charts for the laptop](docs/screenshots/hosts.png)

Loaded models and their VRAM use come from Ollama's API, so they work for remote backends too. CPU and GPU load need a process on the machine itself:

- **On the proxy's own machine**, metrics are collected automatically. GPU figures need `nvidia-smi`.
- **On a remote machine**, install the proxy package there and run `agent-metrics`, which listens on port 8182. Then set that backend's **Metrics URL** in Settings, for example `http://192.168.1.133:8182`.

## Reviewing requests

The **Requests** page searches the full history and filters it by session, saved payloads or failures. Select a request to see:

- time to first token and the prompt and generation rates;
- the context size;
- when payloads were captured, the conversation, the raw request and response, and the exact payload sent to Ollama.

![Request history with the inspector open on a Claude Code request](docs/screenshots/requests.png)

Payload capture is off by default because traces can contain source code and secrets. To turn it on for future requests, use `--trace`, the **Capture** switch on **Sessions**, or **Enable tracing** in the inspector. The UI has no authentication, so keep the proxy on localhost or a trusted network.

The console follows your system theme, or you can pick Light or Dark.

![Overview in dark mode](docs/screenshots/overview-dark.png)

## Configuration

[`config/backends.yaml`](config/backends.yaml) holds the network settings. You can also edit them on the **Settings** page:

```yaml
proxy:
  listen_host: 127.0.0.1    # 0.0.0.0 to accept other machines
  listen_port: 8181
  url: http://127.0.0.1:8181 # address the launcher gives agents
default_backend: laptop
backends:
  laptop:  {type: ollama, url: http://localhost:11434}
  desktop: {type: ollama, url: http://192.168.1.133:11434}
```

Each backend can also have an optional `metrics_url` (see [Watching the Ollama machines](#watching-the-ollama-machines)).

Environment overrides: `AI_PROXY_LIVE` (0 hides live text), `AI_PROXY_HOST`, `AI_PROXY_PORT`, `AI_PROXY_URL`, `AI_PROXY_DEFAULT_BACKEND`, `AI_PROXY_LOG_DIR`, `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS`, `AI_PROXY_AGENTS`, `AI_PROXY_UI_DIR`, and `AI_PROXY_HOME` (repository root).

## API

Agent-facing, either per session or unscoped (unscoped requests get an implicit session, or set `X-AI-Proxy-Session`):

| Path | API |
|---|---|
| `/session/{id}/v1/...` | OpenAI: chat completions (translated), responses, models, embeddings |
| `/session/{id}/anthropic/v1/messages` | Anthropic Messages, plus `count_tokens` |
| `/session/{id}/api/...` | Native Ollama pass-through, with profiles applied to `chat` and `generate` |

Management, used by the UI and launcher (interactive docs at `/docs`):

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Totals, in-flight requests, recent requests |
| `GET /api/requests[?session_id=]` | Full request history with payload availability |
| `POST /api/sessions`, `GET /api/sessions` | Open or list sessions |
| `PATCH /api/sessions/{id}` | End a session or toggle capture |
| `DELETE /api/sessions/{id}[/data]` | Delete a session, or only its saved data |
| `GET /api/sessions/{id}/telemetry`, `.../traces/{request}` | JSONL telemetry and captured payloads |
| `GET /api/agents`, `POST /api/agents/{id}/sessions` | Agent registry; open a session and get its environment |
| `GET /api/models` | Models per backend with loaded state and applied profile |
| `GET /api/live[?session_id=]` | Server-sent events: snapshot, then start, delta and end events for streamed requests, optionally for one session |
| `GET /api/hosts/{backend}` | Loaded models, machine load and traffic for one backend, sampled now |
| `GET /api/host/metrics` | CPU, memory and GPU usage of this machine (also served by `agent-metrics`) |
| `GET /api/config`, `PUT /api/config` | Network configuration |

## Benchmarks

Add a folder under `benchmarks/tests` with `prompt.md` and `test.yaml`, then:

```bash
agent-benchmark reasoning --model qwen3.6:35b --backend desktop
agent-benchmark --all
```

Each run gets its own session. Results are written to `benchmarks/results/<timestamp>`.

## Development

```bash
./scripts/dev.sh          # or .\scripts\dev.ps1: API with reload + Vite on :5173
cd proxy && pytest        # proxy tests
cd ui && npm run typecheck
```

Logs and traces are stored under `logs/`:

```text
logs/
├── proxy/proxy.log
└── sessions/<session-id>/
    ├── session.json
    ├── requests.jsonl
    └── traces/000001.json
```

To regenerate the screenshots, run `scripts/screenshots/traffic.py` against a proxy to create sample traffic, then `scripts/screenshots/capture.mjs`.
