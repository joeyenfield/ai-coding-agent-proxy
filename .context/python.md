# Python client and machine (`python/agent_py/`)

Drop-in Python versions (3.9+) of `agent`, `agent-proxy` and `agent-machine`, for computers without a Go build. Same command names, protocol, files and ports. The distribution is `ai-proxy-agent-py`; the server hands it out as a wheel built by `tools/dist/python.go`.

## Design in one paragraph

It mirrors the Go programs behaviour for behaviour, and the Go code is the reference. The proof is `server/tests/test_client.py`: `python/tests` runs the same contract tests against these programs. Unlike Go, the client and machine share code (`common.py`, `login.py`), but they keep separate credentials. Nothing is left out: containers, starting Strata and Ollama's systemd service, every provider op and the catalogue searches are all here.

## Map

| Module | Mirrors | Notes |
| --- | --- | --- |
| `common.py` | `client/internal/settings`, `version.go` | `PROTOCOL`, headers, `client()` (httpx `trust_env=False`, the `Proxy: nil` equivalent), credentials (`Credentials`, `Upstream(use_env=...)`), `look_path`, `checkout_version` (kept in step with `version.py` and `version.go`) |
| `login.py` | `client/internal/login`, `machine/internal/login` | One implementation, called with each program's credentials path |
| `oauth.py` | Both Go settings OAuth implementations | Device grants, endpoint validation, cross-process refresh serialization, atomic rotating credential persistence and revocation; no legacy password/key fallback |
| `update.py` | `internal/update` | Pip-installs the server's wheel (SHA-256 checked), prunes other versions, checks the import in a clean environment from home |
| `client/launcher.py` | `internal/launcher` | `parse_args`, `ensure_proxy`, `launch`, `run_headless`, `launch_shell`, `ensure_container` |
| `client/localproxy.py` | `internal/localproxy` | A Starlette/ASGI `AgentProxy` on uvicorn plus an asyncio `TunnelServer` on 8183 in the same loop. `LAUNCHER_API` uses the same regexes as Go's `LauncherAPI`; agents' request bodies stream through |
| `client/terminal.py` | `internal/terminal` | stdlib `pty` on POSIX, pywinpty ConPTY on Windows; the same Share protocol and timings |
| `machine/__init__.py`, `settings.py` | `machine/internal/command`, `settings` | `run`/`start`/`stop`, toggles (launches, servers, commands, updates, roots, name, address), `containers on/off/cli`, `servers service [off]` |
| `machine/presence.py` | `machine/internal/presence` | Fleet socket, hello and updates, tunnels, launches, shells, folders, remote update |
| `machine/servers.py` | `machine/internal/servers` | Ollama start/stop/restart (`ollama serve` in its own session, reaped by a thread, or `ollama.service` through `sudo -n systemctl` once `servers service` allows it), `ollama.json` settings (the same table as `ollama_settings.go`), `servers.json`, `ollama.log`; Strata profiles (`strata runtime start/stop --profile`, `strata_state()`); `AI_PROXY_STRATA_COMMAND`/`AI_PROXY_SYSTEMCTL_COMMAND` swap in fakes |
| `machine/providers.py` | `machine/internal/providers` | `PROBES` (ollama 11434, docker 12434, strata 18500), `detect`/`with_strata`/`with_keys` (presence/providers.go), `Manager.handle` (the op table of providers.go), Strata profiles and instances, metrics (Go's sampling; psutil only where Go reads nothing) |
| `machine/catalog.py` | `providers/catalog.go` | ollama.com, Hugging Face and Docker Hub searches (`Catalogs`, the hub cache) |
| `machine/containers.py` | `machine/internal/containers` | The engine table, `validate`, `run_args_for`, `CLI`/`detect` (Docker if answering, else Podman), `Manager` (jobs; the hello's `containers`, `container_result`) |
| `machine/mcp.py` | `machine/internal/mcp` | `Bridge` on uvicorn at `127.0.0.1:0` with a per-run key, a stdio process per MCP session, 6 h idle reap |

## What's different

Only how, never what:

- Updates pip-install the wheel instead of swapping binaries. `agent-machine update --with-agent` runs `AI_PROXY_AGENT update --server` first when that's set, as Go does; otherwise the agent is this same package and only its agent-proxy is stopped.
- A launch runs `AI_PROXY_AGENT` or the Python agent, so the hello's `launcher` is always true.
- Error texts from the transport are httpx's, and timeouts say "timed out after Ns", not Go's context errors.

## Rules

- **Same functionality as Go.** Every feature of `client/` and `machine/` is here too, added in the same change, with tests mirroring the Go ones. The Go code is the reference.
- **Any API change between the client or machine and the server is made in Go and here**, and covered by `test_client.py` (which runs against both).
- Don't install both the Go and the Python programs on one computer: whichever is first on PATH wins.
- Python imports `agent_py` from the cwd first, so update and restart run from home, not a checkout.

## Tests

Plain launches catch SIGINT in the parent while the agent runs, then restore the old
handler. Do not set SIG_IGN: POSIX children inherit it and lose default Ctrl+C handling.
`test_units.py` verifies the child still exits on SIGINT.

`cd python && python -m pytest -q`. `tests/conftest.py` puts `server/tests` on the path and makes real console scripts for the `agent_binary`/`machine_binary` fixtures. `test_contract.py` imports the shared tests from `server/tests/test_client.py` and adds Python-specific variants (the Ollama probe redirected through a generated `sitecustomize.py`). `test_update.py` covers the wheel update; its programs run on a throwaway virtualenv's Python (`AI_PROXY_AGENT` too), never the one running the tests. `test_units.py`, `test_catalog.py`, `test_providers.py`, `test_containers.py` and `test_servers.py` mirror the Go unit tests. `conftest.py` defaults `AI_PROXY_STRATA_COMMAND` and `AI_PROXY_SYSTEMCTL_COMMAND` to `[]`, so a real Strata or `ollama.service` on the computer is never used.
