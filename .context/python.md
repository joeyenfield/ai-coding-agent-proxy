# Python client and machine (`python/agent_py/`)

Drop-in Python versions (3.9+) of `agent`, `agent-proxy` and `agent-machine`, for computers without a Go build. Same command names, protocol, files and ports. The distribution is `ai-proxy-agent-py`; the server hands it out as a wheel built by `tools/dist/python.go`.

## Design in one paragraph

It mirrors the Go programs behaviour for behaviour, and the Go code is the reference. The proof is `server/tests/test_client.py`: `python/tests` runs the same contract tests against these programs. Unlike Go, the client and machine share code (`common.py`, `login.py`), but they keep separate credentials. The machine side deliberately leaves out containers and starting model servers, and says so in its hello, so the server and console adapt.

## Map

| Module | Mirrors | Notes |
| --- | --- | --- |
| `common.py` | `client/internal/settings`, `version.go` | `PROTOCOL`, headers, `client()` (httpx `trust_env=False`, the `Proxy: nil` equivalent), credentials (`Credentials`, `Upstream(use_env=...)`), `look_path`, `checkout_version` (kept in step with `version.py` and `version.go`) |
| `login.py` | `client/internal/login`, `machine/internal/login` | One implementation, called with each program's credentials path |
| `update.py` | `internal/update` | Pip-installs the server's wheel (SHA-256 checked), prunes other versions, checks the import in a clean environment from home |
| `client/launcher.py` | `internal/launcher` | `parse_args`, `ensure_proxy`, `launch`, `run_headless`, `launch_shell`, `ensure_container` (the launcher side of containers *is* present) |
| `client/localproxy.py` | `internal/localproxy` | A Starlette/ASGI `AgentProxy` on uvicorn plus an asyncio `TunnelServer` on 8183 in the same loop. `LAUNCHER_API` uses the same regexes as Go's `LauncherAPI` |
| `client/terminal.py` | `internal/terminal` | stdlib `pty` on POSIX, pywinpty ConPTY on Windows; the same Share protocol and timings |
| `machine/__init__.py`, `settings.py` | `machine/internal/command`, `settings` | `run`/`start`/`stop`, toggles (launches, servers, commands, updates, roots, name, address); `containers` is refused |
| `machine/presence.py` | `machine/internal/presence` | Fleet socket, hello and updates, tunnels, launches, shells, folders, remote update |
| `machine/providers.py` | `machine/internal/providers` | `PROBES` (ollama 11434, docker 12434, strata 18500), `Manager.handle` → `{kind}_{op}`, metrics (psutil optional) |
| `machine/mcp.py` | `machine/internal/mcp` | `Bridge` on uvicorn at `127.0.0.1:0` with a per-run key, a stdio process per MCP session, 6 h idle reap |

## What's different

- Hello: `containers.enabled=False`, `servers.enabled=False`. `container` and `server` requests get `ok:false`. No Strata load, unload or logs, no catalogue search.
- `AI_PROXY_URL`/`AI_PROXY_TOKEN` apply to the client only (`Upstream(use_env=False)` for the machine).
- The agent-traffic request body is buffered (the response still streams).
- `look_path`'s WSL check drops `/mnt/`, `/c/`, `/d/` PATH entries rather than reading `/proc/mounts`.
- `agent update` uses the sign-in or `AI_PROXY_URL` server, not the running proxy's.

## Rules

- **Any API change between the client or machine and the server is made in Go and here**, and covered by `test_client.py` (which runs against both).
- Don't install both the Go and the Python programs on one computer: whichever is first on PATH wins.
- Python imports `agent_py` from the cwd first, so update and restart run from home, not a checkout.

## Tests

`cd python && python -m pytest -q`. `tests/conftest.py` puts `server/tests` on the path and makes real console scripts for the `agent_binary`/`machine_binary` fixtures. `test_contract.py` imports the shared tests from `server/tests/test_client.py` and adds Python-specific variants (the Ollama probe redirected through a generated `sitecustomize.py`). `test_update.py` covers the wheel update.
