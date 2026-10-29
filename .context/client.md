# Client (`client/`)

The Go module `ai-coding-agent-proxy/client`. It builds `agent` (the launcher, `login`/`logout`, `proxy`, `update`, `version`) and a standalone `agent-proxy`. It runs on every computer that starts coding agents.

## Design in one paragraph

The client is deliberately dumb. It knows the server only by URL, and gets everything else (the agent, its environment, its command, its files, the backend) from the API. The `agent-proxy` is a loopback-only gateway that holds the computer's sign-in. It signs the short list of launcher calls with the token, streams agents' model traffic through unsigned (the session id in the URL is the credential), forwards shared MCP URLs with their per-session capability token, tunnels account agents' HTTPS to the server over WebSockets, and relays shared terminals. The launcher starts the proxy if needed, opens a session, writes the session's files, and runs the agent: plainly, in a shared pseudo-terminal (`--control`), or headless for fleet launches.

## Boundaries

- Never reads `config/` or imports server, machine or Python code. Never chooses backends or renders templates.
- Never joins the fleet: `agent machine` points at agent-machine and exits.
- Binds `127.0.0.1` only. No HTTP client or dialer uses an environment proxy (`Proxy: nil`).
- Dependencies: gorilla/websocket, x/sys, x/term, creack/pty. Go 1.24 in `go.mod`; build with `GOTOOLCHAIN=local`.

## Shape

```mermaid
flowchart LR
  subgraph PC["Agent computer, 127.0.0.1"]
    L["agent (launcher)"] -->|"LauncherAPI"| P
    A["Coding agent"] -->|"/session/<id>/..."| P
    AA["Account agent"] -->|"CONNECT :8183"| P
    L -.->|"--control: WS .../host"| P
    P["agent-proxy :8184"]
  end
  P -->|"signed with token"| S["agent-server /api/..."]
  P -->|"unsigned, streamed"| S2["agent-server /session/..."]
  P ==>|"WS /api/proxy/tunnel"| S3["agent-server intercept"]
  P ==>|"WS /api/terminals/<id>/host"| S4["agent-server terminals"]
```

## Packages

| Package | Responsibility | Key symbols |
| --- | --- | --- |
| `cmd/agent`, `cmd/agent-proxy` | Dispatch subcommands; the version comes from `-ldflags -X main.version` | `main` |
| `internal/settings` | Ports, server URL lookup, credentials, headers, proxy-less transports | `DefaultServerURL`, `Upstream` (re-reads credentials on every call), `Credentials.Save` (0600, atomic), `Transport`, `ServerClient`, `Identify`, `Protocol` |
| `internal/api` | JSON calls, always through the agent-proxy | `Client`, `Error`, `Health` |
| `internal/launcher` | Argument parsing, launching, the shell, containers | `ParseArgs`, `Launch`, `EnsureProxy`, `EnsureContainer`, `LaunchShell`, `List` |
| `internal/localproxy` | The agent-proxy | `LauncherAPI`, `Proxy.ServeHTTP`, `local`, `rewriteAgent`, `ServeTunnels`, `readHead`, `Dial`, `relay` |
| `internal/login` | `agent login`/`logout`, straight to the server | `Login`, `Logout` |
| `internal/terminal` | `--control` and headless runs | `Run`, `RunHeadless`, `Share`, `History` (4 MiB), `InputGap`, ConPTY (`pty_windows.go`), creack/pty (`pty_unix.go`) |
| `internal/process` | Detached starts, PATH lookup that skips Windows drives on WSL | `Detach`, `LookPath`, `AgentDirs` |
| `internal/update` | Self-update from the server's `/downloads/index.json` | `Fetch`, `Plan`, `Apply` (SHA-256), `Swap`, `Cleanup` |

## Flows

**Launch** (`launcher.Launch`)

1. `EnsureProxy`: if `/health` on 8184 doesn't answer, start `agent proxy` detached and wait. If the proxy's server is loopback and down, start `agent-server` from PATH. Version notices go to stderr.
2. `checkSignedIn`, then `findAgent` (`GET /api/agents`), then `process.LookPath` for the executable.
3. Account agents: `GET /api/intercept`, then cache the CA in `~/.cache/agent-proxy/<fp16>/`.
4. `POST /api/agents/{id}/sessions` with model, backend, trace, project, `proxy_url`, `launcher: true`, mcp, sharing, tags and `files_dir`. The reply carries the env, the command, files and the container.
5. Write the files (plain names only) into `~/.cache/agent-proxy/sessions/<rand>/`, removed when the agent exits. Start the container if there is one (`EnsureContainer`; a 409 means use `--switch`).
6. Run: `[resolved exe] + command[1:] + extra`, with the env merged (names match case-insensitively on Windows). **Headless** prints exactly one JSON line, `{"session_id": ...}` (agent-machine reads it), then `RunHeadless`. With `--control`, `terminal.Run` with a `Share`; otherwise `exec.Cmd`.
7. `PATCH /api/sessions/{id}` with `ended` and the exit status.

**agent-proxy** (`Proxy.ServeHTTP`), checked in this order:

1. `GET /health`: open (role, server, user, version, protocol).
2. `POST /shutdown`: `local()` only.
3. `/api/terminals/<id>/host`: WebSocket relay to the server. Refusals map to close `4400+status%100`.
4. Other `/api/*`: `local()`, then the **`LauncherAPI` allowlist** (anything else is a 404). Signed with `X-AI-Proxy-Token`; cookie, Origin and proxy headers are stripped. `/api/intercept`'s `url` is rewritten to the local 8183.
5. `/session/`, `/v1/`, `/anthropic/`: `local()`, then `ReverseProxy` to the same path, **without the token**, flushed immediately for SSE. Bare `/api/*` is *not* agent traffic here; native Ollama calls use `/session/<id>/api/...`.

**HTTPS tunnel** (8183): one `WS /api/proxy/tunnel` per TCP connection, signed. A refused or failed dial reads the agent's CONNECT head first (`readHead`, because Windows resets sockets closed with unread data), then answers 407 or 502.

**Login**: `GET /api/auth/status`; with accounts, `POST /api/auth/token` with username and password, then store `{url, user, token, token_id}` as the default. Logout revokes the token on a best-effort basis.

## State

- `credentials.json` in `%APPDATA%\agent-proxy\` or `~/.config/agent-proxy/` (`AI_PROXY_CREDENTIALS`), with `agent-proxy.log` beside it. The format is shared with the Python client.
- The CA cache and per-session file folders under `~/.cache/agent-proxy/`.
- The proxy keeps no state of its own; it re-reads credentials on every request, so `agent login`/`logout` apply at once.

## Invariants

- `local(r)` = loopback Host **and** same origin (Origin missing or equal to Host), on every route except `/health`.
- The token goes only on `LauncherAPI` forwards and the proxy's own WebSocket dials (tunnel, terminal host), never on agent traffic.
- `Proxy: nil` everywhere (`settings.Transport`, `localproxy.Dial`, `Share`). Started from inside an intercepted agent, it would otherwise loop.
- Terminal input: text and Enter go as separate writes `InputGap` apart (Claude Code reads them together as a paste).
- A `Share` stops retrying only on close 4401, 4403 or 4404, so the terminal relay sends those for real refusals only (HTTP 401/403 from the server, or the server's own close); any other HTTP answer (a reverse proxy's 502/503/504 during a restart) goes to the launcher as 1011. Between tries it calls `Share.Revive` (`launcher.proxyReviver`), which starts the agent-proxy again if it's gone (`agent update` stops it).

## Extending and testing

- **A new server call from the launcher**: add it to the server's API, to `LauncherAPI` here **and** `LAUNCHER_API` in `python/agent_py/client/localproxy.py`, and a test in `server/tests/test_client.py`.
- Go tests: `go test ./...`; `go vet ./...` and `GOOS=linux go vet ./...` for both pty paths.
- The contract: `server/tests/test_client.py` builds `./cmd/agent` and runs it against a real server on free ports.
- Never test on 8183, 8184 or 9999 on the dev machine. Set `AI_PROXY_LOCAL_PORT`, `AI_PROXY_LOCAL_INTERCEPT_PORT`, `AI_PROXY_URL`, `AI_PROXY_CREDENTIALS`.

## Gotchas

- Unknown flags (typos included) are passed to the agent silently.
- `agent proxy` reports `Program="agent"`; only the standalone binary says `agent-proxy` in its User-Agent.
