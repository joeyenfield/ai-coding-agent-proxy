"""The settings a new server starts with: its agents, Ollama model tuning and network defaults.

A fresh database is seeded from these (config.seed). Agents marked builtin follow this file
on every upgrade until an admin edits them; nothing here overrides what an admin changed.
The definitions are kept as YAML text so their comments stay beside them; the server stores
and serves them as JSON.
"""

from __future__ import annotations

from typing import Any

import yaml

# Where a server started without stored settings listens, and how agent machines reach it.
PROXY: dict[str, Any] = {"listen_host": "0.0.0.0", "listen_port": 9999, "url": "http://127.0.0.1:9999"}
INTERCEPT: dict[str, Any] = {"enabled": True, "passthrough": []}
BACKENDS: dict[str, dict[str, Any]] = {"anthropic": {"type": "anthropic", "url": "https://api.anthropic.com"}}
DEFAULT_BACKEND = "anthropic"
# Traces and terminal recordings of sessions older than this are deleted (None keeps them).
RETENTION_DAYS: int | None = None

MODELS_YAML = r"""
# The Ollama model tuning a new server starts with: settings the proxy applies
# before forwarding a request to Ollama. Admins change the stored copy on Server
# settings -> Ollama model tuning (or PUT /api/config/models).
#
# Settings are layered: defaults, then the first matching profile (shell-style
# pattern matched against the Ollama model name), then the exact alias. Values a
# client sends explicitly in a request always win.
#
# options       Ollama runtime options (num_ctx, temperature, top_p, top_k, ...)
# keep_alive    how long Ollama keeps the model loaded between requests
# think         true/false to force thinking on or off for hybrid models
# translate_openai
#               route OpenAI Chat Completions through native /api/chat so these
#               settings apply. Ollama's /v1 endpoint ignores num_ctx, which
#               silently truncates long agent conversations to the server default.

defaults:
  keep_alive: 30m
  translate_openai: true
  options:
    # Coding agents send large system prompts and tool schemas. 64K is a good
    # balance for 16-24 GB GPUs; raise it to 131072 if you have the memory.
    num_ctx: 65536

profiles:
  # Qwen coder variants (qwen3-coder, qwen3.6:27b-coding, ...): Qwen's
  # recommended non-thinking sampling for agentic coding.
  - name: qwen-coder
    match: ["qwen3*coder*", "qwen3*coding*"]
    options:
      temperature: 0.7
      top_p: 0.8
      top_k: 20
      repeat_penalty: 1.05

  # Qwen 3.x hybrid reasoning models (qwen3, qwen3.5, qwen3.6, qwen3.8, ...).
  # These are Qwen's recommended thinking-mode sampling parameters.
  - name: qwen3
    match: ["qwen3*"]
    options:
      temperature: 0.6
      top_p: 0.95
      top_k: 20
      min_p: 0

# Friendly aliases. The key is the name agents use, ollama_model is the tag.
aliases:
  qwen-fast:
    # Same weights with thinking disabled for quicker edit loops.
    ollama_model: qwen3.6:35b
    think: false
    options:
      temperature: 0.7
      top_p: 0.8
"""

AGENTS_YAML = r"""
# Coding agents the launcher and dashboard know how to connect to the proxy.
#
# These are the agents that ship with the agent-server. A new server stores them in
# its database, and an upgrade brings in changes to the ones no admin has changed.
# Admins add, edit, reset and remove agents on Server settings -> Agents (or with
# /api/config/agents/<id>). Launchers (the client's `agent`) get the stored agents
# (GET /api/agents) and the server renders these templates for each session, so a
# new or changed agent is available on every agent machine at once; only its
# executable has to be installed there.
#
# Every request an agent makes is routed through a session, to its machine's
# agent-proxy (http://127.0.0.1:8184), which carries it to the agent-server.
# Strings in env and args are templates; available placeholders:
#
#   {openai_url}     http://127.0.0.1:8184/session/<id>/v1          OpenAI Chat / Responses
#   {anthropic_url}  http://127.0.0.1:8184/session/<id>/anthropic   Anthropic Messages
#   {ollama_url}     http://127.0.0.1:8184/session/<id>             native Ollama (/api/...)
#   {model} {session_id} {proxy_url} {project}
#   {context_window}  the model's context size on the session's backend (Ollama's num_ctx
#                    from the model tuning, or what Strata or Docker Model Runner reports); as a
#                    whole value it renders as a number
#   {files_dir}      the folder the launcher writes this agent's files to, for this session
#   {intercept_url} {ca_cert} {ca_bundle}           only for intercept: true
#                    ca_bundle is the system roots plus the server's CA
#
# An env value written as a YAML mapping or list is rendered and passed as JSON.
# files: maps a file name to its contents (a mapping is written as JSON); the launcher
# writes them to {files_dir} before starting the agent and removes them after. A value
# whose placeholder is unknown for the session (no context size, or the console rather
# than a launcher asking) is left out, so the agent keeps its own default.
# protocol: openai-chat | openai-responses | anthropic | ollama
# backend_type: ollama (default) | anthropic - which of the server's providers
# (backends) the agent can use; the launcher picks a matching backend when --backend is omitted.
# ollama agents can also use a strata or docker (Docker Model Runner) backend,
# e.g. --backend strata, unless their protocol is ollama and the backend is
# Strata: Strata has no native Ollama API. Docker Model Runner has all four.
#
# install is the command that installs the agent: one string for everywhere, or one per
# environment (linux, macos, windows, wsl; wsl falls back to linux, and default covers the
# rest). The console's Start an agent page shows it for the environment you pick.
#
# mcp: how the agent takes the MCP servers machines share (agent-machine mcp add) that its
# launch chose (agent ... --mcp, or Start an agent); without it, it can't have any.
#   entry      one server as the agent's config wants it: {mcp_name}, {mcp_url}
#              {mcp_url} is a Streamable HTTP URL with a per-session capability token
#   {mcp_servers}  anywhere in env, files or args: every chosen server's name mapped to its entry,
#              or nothing without any, so the key holding it is left out
#   env, files, args  added only when the launch has MCP servers; args_each once for each server
#              They go where the agent's args have "{mcp_args}", or else before its own args.
#
# intercept: true leaves the agent talking to its own service with its own
# login, and sends all of its HTTPS to its machine's agent-proxy, which tunnels
# it to the server (HTTPS_PROXY plus the server's CA, trusted only by the
# launched process tree). Every request it makes, to any host, is recorded in
# the session.

agents:
  qwen:
    name: Qwen Code
    executable: qwen
    protocol: openai-chat
    description: Alibaba's terminal agent, tuned for the Qwen tool-calling format.
    install: npm install -g @qwen-code/qwen-code
    homepage: https://github.com/QwenLM/qwen-code
    recommended_for: [qwen]
    env:
      OPENAI_BASE_URL: "{openai_url}"
      OPENAI_API_KEY: local-ollama
      OPENAI_MODEL: "{model}"
      # Qwen Code takes its context size from the model's name (any qwen3.x is 1M), so tell it the real one.
      QWEN_CODE_SYSTEM_SETTINGS_PATH: "{files_dir}/qwen-settings.json"
    files:
      qwen-settings.json:
        model:
          generationConfig:
            contextWindowSize: "{context_window}"
        mcpServers: "{mcp_servers}"
    mcp:
      entry: {httpUrl: "{mcp_url}"}
    args: [--auth-type, openai, --model, "{model}"]

  opencode:
    name: OpenCode
    executable: opencode
    protocol: openai-chat
    description: Open-source terminal agent with first-class support for local providers.
    install:
      linux: curl -fsSL https://opencode.ai/install | bash
      macos: curl -fsSL https://opencode.ai/install | bash
      # The script installs into WSL rather than Windows when WSL is there; npm doesn't.
      windows: npm install -g opencode-ai
    homepage: https://opencode.ai
    recommended_for: [qwen, gpt-oss, devstral]
    env:
      OPENCODE_CONFIG_CONTENT:
        $schema: https://opencode.ai/config.json
        model: "agent-proxy/{model}"
        provider:
          agent-proxy:
            npm: "@ai-sdk/openai-compatible"
            name: AI Control Mesh (Ollama)
            options:
              baseURL: "{openai_url}"
              apiKey: local-ollama
            models:
              "{model}":
                name: "{model}"
                tool_call: true
        mcp: "{mcp_servers}"
    mcp:
      entry: {type: remote, url: "{mcp_url}", enabled: true}
    args: [--model, "agent-proxy/{model}"]

  copilot:
    name: GitHub Copilot CLI (Ollama, BYOK)
    executable: copilot
    protocol: openai-chat
    description: GitHub's terminal agent, using bring-your-own-model mode.
    install: npm install -g @github/copilot
    homepage: https://docs.github.com/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
    env:
      COPILOT_PROVIDER_TYPE: openai
      COPILOT_PROVIDER_BASE_URL: "{openai_url}"
      COPILOT_PROVIDER_API_KEY: local-ollama
      COPILOT_MODEL: "{model}"
      COPILOT_OFFLINE: "true"
    mcp:
      entry: {type: http, url: "{mcp_url}", tools: ["*"]}
      files:
        copilot-mcp.json: {mcpServers: "{mcp_servers}"}
      args: [--additional-mcp-config, "@{files_dir}/copilot-mcp.json"]
    notes: COPILOT_OFFLINE stops the CLI from contacting GitHub. Remove it to keep GitHub sign-in features.

  claude:
    name: Claude Code (Ollama)
    executable: claude
    protocol: anthropic
    description: Anthropic's agent, translated from the Messages API to Ollama by the proxy.
    install:
      linux: curl -fsSL https://claude.ai/install.sh | bash
      macos: curl -fsSL https://claude.ai/install.sh | bash
      windows: irm https://claude.ai/install.ps1 | iex
    homepage: https://docs.anthropic.com/claude-code
    env:
      ANTHROPIC_BASE_URL: "{anthropic_url}"
      ANTHROPIC_AUTH_TOKEN: local-ollama
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_MODEL: "{model}"
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "{model}"
      ANTHROPIC_DEFAULT_SONNET_MODEL: "{model}"
      ANTHROPIC_DEFAULT_OPUS_MODEL: "{model}"
      CLAUDE_CODE_SUBAGENT_MODEL: "{model}"
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
    mcp:
      entry: {type: http, url: "{mcp_url}"}
      files:
        claude-mcp.json: {mcpServers: "{mcp_servers}"}
      args: [--mcp-config, "{files_dir}/claude-mcp.json"]
    args: [--model, "{model}"]

  claude-subscription:
    name: Claude Code (Anthropic API pass-through)
    executable: claude
    protocol: anthropic
    description: Claude Code using its saved claude.ai login through an Anthropic pass-through backend.
    install:
      linux: curl -fsSL https://claude.ai/install.sh | bash
      macos: curl -fsSL https://claude.ai/install.sh | bash
      windows: irm https://claude.ai/install.ps1 | iex
    homepage: https://code.claude.com/docs/en/llm-gateway
    backend_type: anthropic
    env:
      ANTHROPIC_BASE_URL: "{anthropic_url}"
    mcp:
      entry: {type: http, url: "{mcp_url}"}
      files:
        claude-mcp.json: {mcpServers: "{mcp_servers}"}
      args: [--mcp-config, "{files_dir}/claude-mcp.json"]
    args: [--model, "{model}"]
    notes: Sign in with claude first; unset ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN and remove any apiKeyHelper to keep subscription authentication.

  claude-account:
    name: Claude Code (claude.ai account)
    executable: claude
    protocol: anthropic
    intercept: true
    description: Claude Code signed in to your Claude account. All of its HTTPS traffic - messages, token counts, OAuth refresh, telemetry, MCP and web fetches - is recorded.
    install:
      linux: curl -fsSL https://claude.ai/install.sh | bash
      macos: curl -fsSL https://claude.ai/install.sh | bash
      windows: irm https://claude.ai/install.ps1 | iex
    homepage: https://code.claude.com/docs/en/network-config
    mcp:
      entry: {type: http, url: "{mcp_url}"}
      files:
        claude-mcp.json: {mcpServers: "{mcp_servers}"}
      args: [--mcp-config, "{files_dir}/claude-mcp.json"]
    notes: Sign in with claude first. Unset ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL, and remove any apiKeyHelper, so the account login is used. Pass a model with -- --model sonnet.

  copilot-account:
    name: GitHub Copilot CLI (GitHub account)
    executable: copilot
    protocol: openai-chat
    intercept: true
    description: Copilot CLI signed in to your GitHub Copilot subscription. All of its HTTPS traffic to GitHub and Copilot is recorded.
    install: npm install -g @github/copilot
    homepage: https://docs.github.com/copilot/how-tos/copilot-cli
    mcp:
      entry: {type: http, url: "{mcp_url}", tools: ["*"]}
      files:
        copilot-mcp.json: {mcpServers: "{mcp_servers}"}
      args: [--additional-mcp-config, "@{files_dir}/copilot-mcp.json"]
    notes: Sign in with copilot (/login) first. Unset COPILOT_PROVIDER_* and COPILOT_OFFLINE so the GitHub subscription is used. Pass a model with -- --model <name>.

  codex-account:
    name: OpenAI Codex CLI (ChatGPT account)
    executable: codex
    protocol: openai-responses
    intercept: true
    description: Codex signed in with your ChatGPT plan. All of its HTTPS and WebSocket traffic to OpenAI and ChatGPT is recorded.
    install: npm install -g @openai/codex
    homepage: https://developers.openai.com/codex/cli
    env:
      # Codex replaces its trusted roots with this file, so give it the full bundle.
      CODEX_CA_CERTIFICATE: "{ca_bundle}"
    # The interactive TUI otherwise hands conversations to a shared background
    # app-server daemon, which runs without the proxy settings above.
    args: [--no-daemon]
    mcp:
      args_each: [-c, 'mcp_servers.{mcp_name}.url="{mcp_url}"']
    notes: Sign in with codex login (ChatGPT) first, and unset OPENAI_API_KEY and OPENAI_BASE_URL so the ChatGPT login is used. Pass a model with -- -m <model>.

  codex:
    name: OpenAI Codex CLI (Ollama)
    executable: codex
    protocol: openai-responses
    description: OpenAI's agent in --oss mode. Uses the Responses API, passed through to Ollama.
    install: npm install -g @openai/codex
    homepage: https://github.com/openai/codex
    env:
      CODEX_OSS_BASE_URL: "{openai_url}"
    mcp:
      args_each: [-c, 'mcp_servers.{mcp_name}.url="{mcp_url}"']
    args: [--oss, -m, "{model}"]
    notes: The Responses API is passed through unchanged, so set OLLAMA_CONTEXT_LENGTH on the Ollama server for a larger context.

  aider:
    name: Aider
    executable: aider
    protocol: ollama
    description: Pair-programming agent that talks to Ollama's native chat API.
    install: python -m pip install aider-install && aider-install
    homepage: https://aider.chat
    env:
      OLLAMA_API_BASE: "{ollama_url}"
    args: [--model, "ollama_chat/{model}"]

  goose:
    name: Goose
    executable: goose
    protocol: ollama
    description: Block's extensible open-source agent.
    install:
      linux: curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash
      macos: curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash
      # In Git Bash (it needs bash, curl and unzip).
      windows: curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash
    homepage: https://block.github.io/goose
    env:
      GOOSE_PROVIDER: ollama
      OLLAMA_HOST: "{ollama_url}"
      GOOSE_MODEL: "{model}"
    mcp:
      args_each: [--with-streamable-http-extension, "{mcp_url}"]
    # The extensions are options of goose session.
    args: [session, "{mcp_args}"]
"""


def models() -> dict[str, Any]:
    """{"defaults": ..., "profiles": [...], "aliases": {...}}: layered by Settings.profile()."""
    data = yaml.safe_load(MODELS_YAML) or {}
    return {"defaults": data.get("defaults") or {}, "profiles": list(data.get("profiles") or []), "aliases": data.get("aliases") or {}}


def agents() -> dict[str, dict[str, Any]]:
    """The shipped agents by id, in order."""
    return dict((yaml.safe_load(AGENTS_YAML) or {}).get("agents") or {})
