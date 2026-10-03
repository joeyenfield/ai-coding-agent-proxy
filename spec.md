# Local AI Agent Framework

## 1. Overview

The goal of this project is to provide a lightweight framework for running and evaluating AI coding agents against local or remotely hosted Ollama models.

The framework will provide three main components:

1. **AI / Ollama Proxy**
2. **Benchmarking Tool**
3. **Agent Launcher**

The proxy is the central component. All AI traffic from agents and benchmark tools should pass through the proxy so that routing, tracing, token accounting, latency measurement, and request logging are handled consistently.

The framework should support both:

- Ollama running locally on the same machine
- Ollama running remotely, for example on a more powerful desktop PC

Initial agent support should include:

- OpenAI Codex CLI
- Claude Code

The architecture should allow additional agents and model backends to be added later.

---

# 2. High-Level Architecture

```text
                    ┌───────────────────────┐
                    │     Agent Launcher    │
                    │                       │
                    │ Codex / Claude Code   │
                    │ Project directory     │
                    │ Model / Backend       │
                    └───────────┬───────────┘
                                │
                         session_id
                                │
                                ▼
┌───────────────────────────────────────────────────────┐
│                     AI Proxy                          │
│                                                       │
│  OpenAI-compatible API       Anthropic-compatible API │
│                                                       │
│          Routing / Logging / Telemetry                │
│                       │                               │
│              ┌────────┴────────┐                      │
│              ▼                 ▼                      │
│        Local Ollama       Remote Ollama               │
│        localhost          Desktop PC                  │
└───────────────────────────────────────────────────────┘
                       ▲
                       │
                Benchmark Tool
```

All model-related traffic should pass through the proxy.

The benchmark tool must therefore benchmark the same request path used by real coding agents.

---

# 3. AI / Ollama Proxy

## 3.1 Purpose

The proxy is responsible for:

- Routing AI requests
- Switching between Ollama backends
- Supporting local and remote Ollama instances
- Capturing request and response telemetry
- Measuring token usage and performance
- Streaming responses to clients
- Recording request traces
- Exposing a basic web UI
- Providing OpenAI-compatible endpoints
- Providing Anthropic-compatible endpoints where required

The proxy should be implemented as a long-running service.

A suitable initial implementation would use Python and FastAPI.

---

# 3.2 Proxy Identity

Each running proxy instance should generate a random UUID:

```text
proxy_instance_id
```

Example:

```text
proxy_instance_id = e491dc20-...
```

This identifies the running proxy process.

The proxy instance ID must be separate from agent session IDs.

---

# 3.3 Sessions

A session represents a single logical invocation of an agent or benchmark.

For example:

```text
agent codex ./bmo-ai
```

creates one session.

Running Codex again creates another session, even if the proxy remains running.

Each session receives a UUID:

```text
session_id
```

Example:

```text
79dba2dd-...
```

Session metadata should include:

```json
{
  "session_id": "79dba2dd",
  "client": "codex",
  "project": "bmo-ai",
  "model": "qwen3.6",
  "backend": "desktop",
  "trace": true,
  "started_at": "2026-10-03T10:00:00Z",
  "ended_at": null,
  "tags": {
    "task": "pygame-development"
  }
}
```

Additional arbitrary tags should be supported.

---

# 3.4 Request IDs

Every request within a session receives a sequential request ID.

Example:

```text
000001
000002
000003
```

The sequence resets for each new session.

The combination of:

```text
session_id + request_id
```

must uniquely identify an AI request.

---

# 3.5 Backend Configuration

Backends should not be hard-coded as simply "local" or "remote".

They should be defined as named profiles.

Example:

```yaml
backends:

  laptop:
    type: ollama
    url: http://localhost:11434

  desktop:
    type: ollama
    url: http://192.168.1.133:11434
```

The user can then select a backend using:

```text
--backend desktop
```

This should allow additional backends to be added later without changing the application architecture.

---

# 3.6 API Compatibility

The proxy should expose an OpenAI-compatible API for clients such as Codex.

Example:

```text
http://localhost:8181/v1/
```

Possible endpoints include:

```text
/v1/chat/completions
/v1/completions
/v1/models
```

Additional endpoints should be implemented as required by the supported agents.

The proxy should also support an Anthropic-compatible API layer where required for Claude Code.

Example:

```text
http://localhost:8181/anthropic/
```

The internal proxy layer should translate requests into the appropriate Ollama API calls.

---

# 3.7 Streaming

Streaming responses are mandatory.

The proxy must forward streaming chunks to the client immediately.

It must not wait for the entire model response before returning data to Codex or Claude.

Conceptually:

```text
Agent
  │
  │ Request
  ▼
Proxy ─────────────► Ollama
  ▲                    │
  │                    │ Token / chunk
  │◄───────────────────┘
  │
  │ Immediately forward
  ▼
Agent
```

Telemetry should be collected without interfering with the stream.

---

# 3.8 Telemetry

The proxy should record the following metrics where available:

```text
request bytes
response bytes

input tokens
output tokens

time to first token
prompt evaluation time
generation time
total request time

prompt tokens per second
generation tokens per second

HTTP status
error type

model
backend
endpoint
client
session ID
request ID

context size
temperature
maximum output tokens
```

Where Ollama provides token counts directly, those values should be treated as authoritative.

Token counts should not be estimated from byte size unless no backend-provided value is available.

---

# 3.9 Structured Logging

Structured telemetry should be stored using JSONL.

Each completed request should generate one JSON object.

Example:

```json
{
  "request_id": 42,
  "timestamp": "2026-10-03T10:14:23.123Z",
  "session_id": "79dba2dd",
  "client": "codex",
  "model": "qwen3.6",
  "backend": "desktop",
  "endpoint": "/v1/chat/completions",

  "request_bytes": 18342,
  "input_tokens": 4312,

  "response_bytes": 5321,
  "output_tokens": 812,

  "ttft_ms": 1382,
  "prompt_eval_ms": 1834,
  "generation_ms": 12540,
  "total_ms": 15756,

  "prompt_tps": 2351.1,
  "generation_tps": 64.75,

  "status": 200
}
```

---

# 3.10 Log Directory Structure

Logs should use the following structure:

```text
logs/
├── proxy/
│   └── proxy.log
│
└── sessions/
    └── <session_id>/
        ├── session.json
        ├── requests.jsonl
        └── traces/
            ├── 000001.json
            ├── 000002.json
            └── 000003.json
```

`proxy.log` contains general proxy lifecycle and diagnostic information.

`session.json` contains session metadata.

`requests.jsonl` contains structured performance information for each request.

---

# 3.11 Full Request Tracing

Tracing should be optional.

It can be enabled:

```text
--trace
```

When tracing is enabled, each request should generate a separate trace file:

```text
logs/sessions/<session_id>/traces/<request_id>.json
```

The trace should contain:

```text
request metadata
full request payload
full response payload
timing information
token information
backend information
errors, if any
```

Tracing should be disabled by default because traces may become very large and may contain source code or other sensitive context.

---

# 3.12 Proxy Web UI

The proxy should expose a basic monitoring interface.

Default URL:

```text
http://localhost:8181/
```

The first version should remain intentionally simple.

The UI should display:

```text
Proxy status

Current backend
Backend URL

Active model
Active sessions

Total requests
Total input tokens
Total output tokens

Current request
Current client
Current context size
Current TTFT
Current generation speed
Generated tokens

Recent requests
```

Example:

```text
AI Proxy
─────────────────────────────────────

Backend        Desktop Ollama
Address        192.168.1.133:11434

Requests       47
Tokens In      283,492
Tokens Out     18,491

Current Request
─────────────────────────────────────

Request        #47
Client         Codex
Model          qwen3.6
Context        14,281 tokens
TTFT           2.81s
Generation     41.7 tokens/sec
Generated      731 tokens

Recent Requests
─────────────────────────────────────

#47 qwen3.6       41.7 t/s     18.2s
#46 qwen3.6       42.2 t/s      8.7s
#45 qwen3.8        5.3 t/s     92.1s
#44 gpt-oss:20b   30.1 t/s     22.5s
```

Basic controls should include:

```text
Select backend

Enable / disable tracing

Create new session

View sessions

View traces

Download telemetry
```

Model selection should generally remain a session/launcher responsibility rather than changing the model underneath an active agent.

---

# 4. Benchmarking Tool

## 4.1 Purpose

The benchmarking tool should provide repeatable testing of models and backends.

The tool should measure both raw inference performance and, eventually, full coding-agent performance.

All benchmark requests must pass through the AI proxy.

The benchmark tool should never communicate directly with Ollama.

---

# 4.2 Benchmark Test Structure

Each test should live inside its own directory.

Example:

```text
benchmarks/
└── tests/
    ├── python-refactor/
    │   ├── prompt.md
    │   └── test.yaml
    │
    ├── code-review/
    │   ├── prompt.md
    │   └── test.yaml
    │
    └── reasoning/
        ├── prompt.md
        └── test.yaml
```

`prompt.md` contains the exact prompt sent to the model.

`test.yaml` defines the benchmark configuration.

Example:

```yaml
name: python-refactor

runs: 3

temperature: 0
max_tokens: 1024

models:
  - qwen3.6
  - qwen3.8
  - gpt-oss:20b

backend: desktop

timeout: 300
```

---

# 4.3 Benchmark Execution

Examples:

```text
python benchmark.py python-refactor
```

Run all tests:

```text
python benchmark.py --all
```

Run against a particular model:

```text
python benchmark.py python-refactor --model qwen3.6
```

Override the backend:

```text
python benchmark.py python-refactor --backend desktop
```

---

# 4.4 Benchmark Runs

By default, each test should run three times.

Example:

```text
run 1
run 2
run 3
```

Each run should create a separate proxy session so individual runs can be inspected independently.

---

# 4.5 Benchmark Metrics

The benchmark should collect:

```text
input tokens
output tokens

time to first token

prompt evaluation time
generation time
total time

prompt tokens/sec
generation tokens/sec

response size

HTTP status
errors
```

Results should include:

```text
average
minimum
maximum
```

for relevant numeric metrics.

---

# 4.6 Benchmark Output

Benchmark results should be stored under a timestamped directory.

Example:

```text
benchmarks/
└── results/
    └── 2026-10-03_10-15-41/
        ├── summary.md
        ├── summary.json
        │
        └── python-refactor/
            ├── run1.md
            ├── run1.json
            ├── run2.md
            ├── run2.json
            ├── run3.md
            └── run3.json
```

Markdown provides human-readable results.

JSON provides machine-readable results for future analysis and graphing.

---

# 4.7 Example Benchmark Summary

```text
# Benchmark Results

Test: python-refactor
Model: qwen3.6
Backend: desktop
Runs: 3

| Metric | Average | Minimum | Maximum |
|---|---:|---:|---:|
| TTFT | 2.18s | 2.01s | 2.41s |
| Prompt TPS | 1835 | 1721 | 1921 |
| Generation TPS | 42.3 | 41.8 | 42.9 |
| Total Time | 19.2s | 18.7s | 19.8s |
| Output Tokens | 722 | 698 | 741 |
```

---

# 5. Agent Benchmarks

Raw inference performance alone does not determine how effective a model is as a coding agent.

A later benchmark mode should therefore measure complete agent tasks.

Example:

```text
benchmarks/
└── agent-tests/
    ├── pygame-bug/
    ├── add-unit-tests/
    ├── implement-feature/
    └── refactor-module/
```

Each benchmark would create a disposable Git repository and ask an agent to complete a defined task.

Metrics should include:

```text
total wall-clock time

number of LLM requests

total input tokens
total output tokens

average TTFT

average generation TPS

files modified

lines modified

tests passed

tests failed

agent exit status
```

This allows comparison of actual agent efficiency.

For example:

```text
Model A

60 tokens/sec
19 model calls
4m 20s total


Model B

35 tokens/sec
7 model calls
2m 10s total
```

Despite being slower at raw token generation, Model B may be substantially faster at completing an agent task.

---

# 6. Agent Launcher

## 6.1 Purpose

The Agent Launcher provides a consistent way of starting supported coding agents through the proxy.

It should remain relatively lightweight.

The launcher is responsible for:

```text
selecting the agent
selecting the project directory
selecting the model
selecting the backend
creating a session
starting the proxy if required
configuring environment variables
launching the agent
recording the agent exit status
```

---

# 6.2 Common CLI

A common command should be provided.

Example:

```text
agent codex .
```

or:

```text
agent claude .
```

Examples with options:

```text
agent codex . --model qwen3.6

agent codex . --model qwen3.6 --backend desktop

agent claude . --model qwen3.6 --backend desktop

agent codex ~/projects/bmo-ai --model qwen3.6 --backend desktop --trace
```

---

# 6.3 Launcher Workflow

When executed, the launcher should:

```text
1. Resolve the project directory

2. Determine the selected backend

3. Check whether the proxy is running

4. Start the proxy if required

5. Generate a session UUID

6. Register the session with the proxy

7. Configure the selected agent to use the proxy

8. Change to the requested project directory

9. Launch the agent

10. Wait for the agent to exit

11. Record exit status and session end time
```

---

# 6.4 Codex Example

Instead of configuring Codex directly against remote Ollama:

```text
Codex
  ↓
192.168.1.133:11434
```

Codex should connect to:

```text
Codex
  ↓
localhost:8181
  ↓
AI Proxy
  ↓
192.168.1.133:11434
```

The launcher should configure the appropriate environment variables before starting Codex.

Example conceptually:

```text
AI_PROXY_SESSION=<uuid>

CODEX_OSS_BASE_URL=http://localhost:8181/v1

codex --oss -m qwen3.6
```

---

# 6.5 Platform Support

Provide launcher wrappers for:

```text
Windows PowerShell
Linux / macOS shell
```

Files:

```text
agent.ps1
agent.sh
```

Both should call the same underlying Python launcher implementation where practical.

This avoids duplicating business logic between operating systems.

---

# 7. Model Configuration

Models may optionally have configuration metadata.

Example:

```yaml
models:

  qwen3.6:
    ollama_model: qwen3.6
    context_size: 32768

  qwen3.8:
    ollama_model: qwen3.8
    context_size: 32768

  gpt-oss-20b:
    ollama_model: gpt-oss:20b
    context_size: 32768
```

This provides a central place for future model-specific configuration.

---

# 8. Proposed Repository Structure

```text
local-agent/
│
├── proxy/
│   ├── app.py
│   ├── config.py
│   ├── router.py
│   ├── telemetry.py
│   ├── sessions.py
│   ├── openai_api.py
│   ├── anthropic_api.py
│   │
│   └── ui/
│       ├── index.html
│       ├── app.js
│       └── style.css
│
├── benchmark/
│   ├── benchmark.py
│   ├── runner.py
│   └── reporter.py
│
├── benchmarks/
│   ├── tests/
│   ├── agent-tests/
│   └── results/
│
├── launcher/
│   ├── agent.py
│   ├── agent.ps1
│   └── agent.sh
│
├── config/
│   ├── backends.yaml
│   └── models.yaml
│
├── logs/
│   ├── proxy/
│   └── sessions/
│
├── tests/
│
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

# 9. Core Design Principles

The following principles should be maintained throughout the implementation.

## Single AI Traffic Path

All model traffic should pass through the proxy:

```text
Benchmark ─────┐
               │
Codex ─────────┼──► AI Proxy ───► Ollama
               │
Claude Code ───┘
```

This ensures all telemetry and routing behaviour is consistent.

---

## Streaming First

The proxy must preserve streaming behaviour and should introduce minimal additional latency.

---

## Structured Telemetry

JSON / JSONL should be the canonical data format.

Markdown and human-readable reports should be generated from structured data rather than used as the primary data source.

---

## Separate Proxy and Session Lifetimes

The proxy may run continuously.

Agent and benchmark sessions should have their own independent lifecycle.

---

## Backend Independence

Agent clients should not need to know where Ollama is running.

They communicate only with the proxy.

The proxy determines the selected backend.

---

## Reproducible Benchmarks

Benchmark prompts, model parameters, backend information, and results should be recorded so that tests can be repeated and compared later.

---

## Extensibility

The framework should make it straightforward to later add:

```text
new agents

new Ollama machines

new model backends

additional API compatibility layers

benchmark visualisation

historical performance comparison

agent task scoring

GPU / CPU telemetry
```

without requiring significant architectural changes.

---

# 10. Initial MVP

The initial MVP should focus on the smallest useful implementation.

## Phase 1 — Proxy

Implement:

```text
FastAPI proxy

OpenAI-compatible streaming endpoint

local / remote Ollama routing

session IDs

sequential request IDs

JSONL telemetry

optional full traces

TTFT measurement

token counts

generation TPS
```

---

## Phase 2 — Benchmark Tool

Implement:

```text
prompt.md tests

test.yaml configuration

three-run benchmarks

proxy-based execution

Markdown results

JSON results

average / minimum / maximum statistics
```

---

## Phase 3 — Agent Launcher

Implement:

```text
Codex launcher

project directory selection

model selection

backend selection

automatic session creation

PowerShell wrapper

shell wrapper
```

---

## Phase 4 — Web UI

Implement:

```text
proxy status

backend information

active sessions

current request

recent requests

token counters

TTFT

generation speed

trace browsing
```

---

## Phase 5 — Additional Agent Support

Add:

```text
Claude Code support

Anthropic-compatible proxy layer
```

---

## Phase 6 — Agent Benchmarking

Add full coding-task benchmarks based on disposable Git repositories.

This phase should measure actual agent effectiveness rather than simply raw model inference performance.