# Working on this project

Notes for coding agents (and people) changing this repository. `README.md` describes what the product does for users; this file covers how the code is put together and how to change it safely. `spec.md` is the original design document and is out of date in places: it predates Strata, Docker Model Runner, account agents and the console's current layout.

## What it is

A proxy that every coding-agent request goes through, in components with a folder each. Keep them separate: they talk only over HTTP and WebSockets, never by importing each other.

- **Server** (`server/`, Python): **`agent-server`** (`agent_server/app.py`) is the central part: the API, accounts, recording, and routing to the providers. It owns all configuration (`config/`), including the agent definitions.
- **Console** (`ui/`, React): the web console. It only calls the server's API; the server serves its build (`ui/dist`).
- **Client** (`client/`, Go): the **`agent`** binary for each agent machine: the launcher, `agent login`/`logout`, and **`agent-proxy`** (`agent proxy`, or `cmd/agent-proxy`), which runs on loopback only. The client knows the server only by URL and gets agents, backends and launch environments from the API. It doesn't join the fleet.
- **Machine** (`machine/`, Go, its own module): the **`agent-machine`** binary, which keeps a computer in the server's **fleet**: `agent-machine login` (its own credentials, apart from `agent login`), `run`/`start`/`stop`, and the machine's settings. Through its fleet connection the server uses the model servers it runs, has it manage them (models, logs, load, Ollama's settings, starting and stopping), runs containers on it and starts agents there (by running the client's `agent`). It shares no code with `client/`: the two talk only to the server, and agent-machine runs `agent` as a program.
- **Android app** (`android/`, Kotlin): a list of servers and each one's console in a WebView. All its features are the console's.
- **Python client and machine** (`python/`, package `agent_py`): `agent-py` and `agent-machine-py`, the same protocol, files and ports as the Go programs, for computers without a Go build. It has no containers, no starting of model servers, and no `update`; its hello says so. `python/tests` runs `server/tests/test_client.py` against it. When you change the API between the client or machine and the server, change it here too.

Agents (Claude Code, Codex, OpenCode, Qwen Code, Aider, Goose, Copilot CLI, ...) are launched pointed at their machine's `agent-proxy` and a **session**. The `agent-proxy` carries everything to the server that machine signed in to with `agent login`. The server forwards each request to an **LLM provider** and records it: tokens, timings, full traces and live streamed text. There are three ways traffic reaches a provider.

**The concept: server, agents, machines.** The server is the hub. Agents connect to it. **Machines** (signed in with `agent-machine login`, kept in the fleet by `agent-machine`) supply every local LLM provider: model servers they run (detected, then adopted by an admin) and containers they run for the server. A local backend (`config.MACHINE_TYPES`: ollama, strata, docker) is never added by URL: `Settings.update_network_config` refuses new or moved ones, and `Backend.machine` names the machine. Hosted APIs (`anthropic`) are the one exception. Backends from before this have no `machine`; they keep working and `Fleet.unclaimed` links them when a machine reports the same model server.

**Agents' traffic to a machine's model server goes through the machine** when it can tunnel (its hello says `"tunnel": 1`) and isn't on the server's computer: `runtime.tunnels.base_url(backend)` gives the routes a loopback listener, each connection there asks the machine (`{"type": "tunnel", "tunnel": id, "port": port}`), and agent-machine dials `WS /api/machines/tunnel/<id>` and pipes raw bytes to `127.0.0.1:<port>` (`machine/internal/presence/tunnel.go`, only to ports it reported or containers' ports). Every new upstream call in `routes/compat.py` must use `base_url`, not `backend.url`. Tests that mock `runtime.client` never reach the listener; `test_client.py` runs a real tunnel.

**The server never manages a model server.** It only forwards agents' traffic to them (`routes/compat.py`). Everything else about an LLM provider (listing models, pull/delete/load/unload, status, logs, the machine's CPU/GPU load, Ollama's `OLLAMA_*` settings, starting and stopping Ollama and Strata, searching ollama.com, Hugging Face and Docker Hub) is a request to the machine that runs it, over the fleet socket, and agent-machine does it there against `127.0.0.1:<port>` (`machine/internal/providers`, `machine/internal/servers`). The console reaches all of it under `/api/machines/{id}/...` (`routes/machines.py`); a provider is named `{kind}-{port}` (`ollama-11434`), and `/api/status` gives each machine backend's `provider`. `/api/models` (the agents' model picker) asks each backend's machine too. Don't add server code that calls a model server for anything but agent traffic: add a provider op to agent-machine and relay it. The native Ollama passthrough only forwards what agents call (`compat.NATIVE_AGENT_PATHS`), so `/api/pull` and friends can't reach a model server through the server either.

| Route | Agents | What the proxy does |
| --- | --- | --- |
| `account` | Agents with `intercept: true` (signed in to their own Claude, Copilot or ChatGPT account) | Nothing is redirected. The agent's HTTPS goes to its machine's `agent-proxy` on 127.0.0.1:8183, which tunnels each connection to the server (`WS /api/proxy/tunnel`). The server decrypts and records it with its CA (`certs/`), which only the launched process tree trusts. Session backend is `direct`. |
| `ollama` (local models) | Agents with `backend_type: ollama` (the default) | Sent to a backend of type `ollama`, `strata` or `docker` (see below). |
| `hosted` | Agents with `backend_type: anthropic` | Passed through to the hosted API unchanged. |

What happens to a request depends on the backend type (`config.BACKEND_TYPES`):

- **`ollama`**: translated and tuned. OpenAI Chat and Anthropic Messages requests are rewritten to native `/api/chat` (`translate/`), with the settings from `config/models.yaml` applied. Ollama's own `/v1` ignores `num_ctx`, which is why the translation exists. Native `/api/*` passes through.
- **`strata`** and **`docker`** (Docker Model Runner): passed through, because they speak the agents' APIs themselves. `config.NATIVE_PROTOCOLS` lists which protocols each one speaks, and `config.serves()` decides which agents can use which backend. Docker paths are remapped by `model_runner.upstream_path()`: `/v1/messages*` goes to `/anthropic/v1/messages*`, other `/v1/*` goes to `/engines/v1/*`, and `/api/*` is unchanged.
- **`anthropic`**: hosted pass-through.

Separately from model traffic, with `--control` the launcher shares the agent's **terminal** with the server, relayed by the `agent-proxy`. Control is off unless asked. The client's `internal/terminal` runs the agent in a pseudo-terminal: ConPTY (through `golang.org/x/sys/windows`) on Windows, a POSIX pty (`creack/pty`) elsewhere. It mirrors that pseudo-terminal to the user's own terminal and streams its output over a WebSocket. The server's `terminals.py` keeps the history and fans it out to console viewers. A viewer's input goes back to the launcher, which types it into the agent. This is the console's **Running agents** page, built so the user can check on agents from a phone.

**Versions.** `VERSION` at the root is the one hand-written version. Builds are `X.Y.Z` on its tag, else `X.Y.Z+<commit>[.dirty]`; tools/dist (`version.go`) and the server (`version.py`) compute it the same way, so keep them in step, and `AI_PROXY_VERSION` overrides both. Compatibility is `PROTOCOL` (an integer, in `version.py` and `settings.Protocol` in client/ and machine/; `test_client.py` checks they agree): `agent` and `agent-machine` send `X-AI-Proxy-Version` and `X-AI-Proxy-Protocol` with everything, and `version.VersionMiddleware` answers 426 (WebSockets: close 4426) outside `MIN_PROTOCOL..PROTOCOL`. Raise `PROTOCOL` for a change older programs can't follow, and `MIN_PROTOCOL` only when the server stops supporting the old one. The server hands out `dist/<its version>/` (`downloads.py`, public `/downloads/...`, only files in that folder's SHA256SUMS); `agent update` and `agent-machine update` take their binaries from its `artifact/` files. `/install.sh` and `/install.ps1` (`installer.py`, templates in `server/agent_server/install/`) are the one-liners: they download the archives for the computer's platform from the table the server writes in, check them, run the archive's own installer (`tools/dist/install/`, with `AI_PROXY_ONE_LINER=1` so it leaves signing in to them) and sign in from the terminal (`/dev/tty`, as their stdin is the download). The address written in is the request's, checked to be plain URL characters (`SAFE_URL`). Test them in a throwaway user in another WSL distro, not as the Ubuntu user.

## Layout

```text
server/                     THE SERVER (Python); console scripts in server/pyproject.toml
  Dockerfile docker/        The server's image (built from the repo root: it needs ui/), its default backends.yaml and entrypoint
server/agent_server/        Python package (FastAPI)
  app.py                    create_app(), route registration order, UI serving, `agent-server` CLI, Supervisor, ensure_first_user
  config.py                 Settings, Backend, backend types, serves()/backend_for(), model profiles, the client's default LOCAL_* ports
  runtime.py                Runtime: shared state (settings, sessions, httpx client, live hub, totals)
  agents.py                 Agent definitions from config/agents.yaml, template rendering (for the console and every client launch)
  terminals.py              Server side of --control: TerminalHub, per-session history, terminal.log/terminal.json, viewer fan-out, attention (waiting for its user, a question read off the screen)
  remote.py                 Who may use terminals: this computer (loopback + loopback Host) or the access key; LAN addresses
  containers.py             Containers machines run for the server: spec checks against the machine's engines, keys, GPU conflicts, phases; asks machines over the fleet
  auth.py                   Accounts: UserStore (users, organizations and their teams, sign-ins, launcher tokens in users.json), Identity, AuthMiddleware, ADMIN_ONLY
  access.py                 Session sharing: who can view/control a session (owner, private/team/org, view/control; teams are <org>/<team>)
  fleet.py                  The fleet: machines' connections, who sees and launches on them, launch records (machines.json in logs/), container requests (Fleet.ask)
  sessions.py telemetry.py  Session store and per-request telemetry written under logs/
  live.py                   Streams in-flight request text to the UI (/api/live, server-sent events)
  chat.py                   A session's traced model calls read back as chats (/api/sessions/{id}/chat): threads by message prefix, replies linked to calls, chat-index.json
  tunnels.py                Agents' requests to machines' model servers through the machines: a loopback listener per (machine, port), one WS /api/machines/tunnel/<id> per connection
  intercept.py certs.py     HTTPS interception (serve_tunnel for agent-proxy tunnels, optional direct listener) and its CA; websocket_tap.py reads WS frames
  translate/                OpenAI Chat and Anthropic <-> native Ollama, including streaming
  strata.py                 The key sent with agents' Strata traffic: a container's, STRATA_API_KEY, or the one its machine reported (kept in memory)
  model_runner.py           Docker Model Runner's path prefixes for agents' traffic
  benchmark.py              `agent-benchmark` runner for benchmarks/tests/<name>/
  ui_build.py               Rebuilds ui/dist on start when UI sources are newer
  version.py downloads.py   The build's version and the protocol gate (426); the builds the server hands out (/downloads/)
  installer.py install/     The one-line installers (/install.sh, /install.ps1): templates filled in with the server's address and its build's archives
  openapi.py                /openapi.json, /docs, /redoc: tags each route by its module (TAGS), unique operation ids, sign-in schemes, the WebSockets in the description
  routes/compat.py          Agent-facing APIs: /v1, /anthropic, /api, plus /session/<id>/... versions
  routes/management.py      Console API: status, config, sessions, requests, traces, live, agents, models, hosts
  routes/machines.py        WS /api/machines/connect, /api/machines (list, launch, adopt a model server, forget), and every LLM provider
                            operation, relayed to the machine: providers/{kind}-{port} (status, models, log), metrics, catalog, servers
  routes/terminals.py       /api/remote (pairing), /api/terminals, WebSockets .../{id}/host (launcher) and .../{id}/view (console)
  routes/proxy.py           WS /api/proxy/tunnel: one account-agent connection from an agent-proxy, into InterceptProxy.serve_tunnel
  routes/containers.py      /api/containers: create on a machine, edit, delete, start/stop/restart (switch), logs; trusted devices, the machine's owner for changes
  routes/auth.py            /api/auth/* (sign-in, tokens, password), /api/orgs/* (organizations, their teams) and /api/users/*
server/tests/               pytest; one file per area (test_proxy, test_units, test_intercept, test_providers, test_strata, test_model_runner, test_terminals, test_containers, test_auth)
  test_client.py            Builds ../client and runs it against a real agent-server: the contract between the two (skipped without Go)
  fake_docker.py            A stand-in docker CLI (state in a JSON file), run by the real client in test_client.py and its Go tests
  conftest.py               Fails (and undoes) any test that changes the real config/*.yaml or creates secrets in certs/
client/                     THE CLIENT (Go module ai-coding-agent-proxy/client); see client/README.md
  cmd/agent/ cmd/agent-proxy/  the binaries: `agent` (subcommands login, logout, proxy, version) and a standalone agent-proxy
  internal/settings/        ports, `agent login`'s credentials.json (same format as before), HTTP clients with Proxy: nil
  internal/api/             JSON calls through the agent-proxy
  internal/localproxy/      agent-proxy: LauncherAPI allowlist, Host/Origin checks, agent traffic (ReverseProxy), terminal relay, HTTPS tunnel
  internal/launcher/        `agent`: argument parsing, Launch (agents, CA copy, session, run), EnsureProxy/agent-server start, EnsureContainer
  internal/login/           `agent login`/`logout`
  internal/terminal/        --control: History, Share (WebSocket), Run, RunHeadless (fleet launches), pty_windows.go (ConPTY), pty_unix.go
  internal/process/         starting programs detached from the terminal
machine/                    AGENT-MACHINE (Go module ai-coding-agent-proxy/machine); see machine/README.md
  cmd/agent-machine/        the binary
  internal/command/         the command line: login, run/start/stop, settings, the status display
  internal/presence/        the fleet connection: Presence (hello/updates, container, server and provider requests, launches), model server detection
  internal/providers/       the model servers here, for the server: status, models (pull streams progress), logs, metrics, catalogue search
  internal/containers/      containers the machine runs for the server: the engine table, spec checks, run args (Podman gets GPUs as CDI devices), the CLI (Detect: Docker if answering, else Podman), jobs
  internal/servers/         starting and stopping the Ollama (`ollama serve`, only one it started; its OLLAMA_* settings in ollama.json) and Strata profiles (`strata runtime`) installed there
  internal/settings/        its folder (AI_PROXY_MACHINE_HOME): credentials.json, machine.json (carried over once from agent-proxy/), status.json, log
  internal/login/ process/  `agent-machine login`/`logout`; detaching, windowless runs, Alive/Stop for start/stop
tools/                      build tooling (Go module ai-coding-agent-proxy/tools): dist/ builds and packs both Go components for every platform (targets and components live here)
  dist/install/             install.sh (Linux, macOS, FreeBSD) and install.ps1 (Windows), embedded into every archive: copy onto PATH, --service, --uninstall
  dist/packages.go rpm.go   -packages: .deb, .apk, Arch and .rpm written in Go; .msi through wix, macOS .pkg through pkgbuild, each skipped without its tool
  dist/python.go            the Python client and machine (python/) as one wheel, written in Go from pyproject.toml, into dist/<version>/python/ (-python=false skips it)
ui/src/                     THE CONSOLE: React 19 + TypeScript + Vite + TanStack Query + react-router
  api.ts                    Every fetch call and every response type
  navigation.ts             The console's three parts (Agents, Server, Machines) and their pages (drives the side rail)
  routes.ts                 Route kinds (account/ollama/hosted) and provider labels
  hooks.tsx live.ts traffic.ts format.ts
                            Shared queries, theme, toasts, clipboard; live SSE; traffic classification; formatting
  pages/                    One file per page; Machine.tsx (a machine and its model servers), Downloads.tsx (/get, public),
                            Terminals.tsx (list, pairing), Terminal.tsx (xterm.js viewer, lazy-loaded), Pair.tsx (/pair#key),
                            Login.tsx (shown instead of the console until signed in), Organization.tsx (account, tokens, organizations, users)
  components/               Shared pieces (Tabs, CodeLine, ConfirmDialog, ModelSearch/usePull, AddBackendForm, ProviderState, PartTag, LogPanel, ...)
  styles.css                The only stylesheet: CSS variables, light and dark themes, no CSS framework
config/                     backends.yaml, models.yaml, agents.yaml
VERSION                     The project's version (X.Y.Z), stamped into every component
compose.yaml .dockerignore  The server in Docker (volumes for config, logs, certs), and the sandbox (--profile sandbox); one build context for both images
sandbox/                    agent-sandbox's Dockerfile and entrypoint: agent + agent-machine + npm coding agents; signs in from env, launches on for /workspace only
scripts/https/              setup-https.ps1 (Windows: nginx + win-acme for Let's Encrypt, firewall, startup task) and the nginx.conf templates it fills in
scripts/                    build-client.ps1/.sh, build-machine.ps1/.sh, build-dist.ps1/.sh (every platform, via tools/dist), agent.ps1/.sh (build and run the client), dev.ps1/.sh, start-ollama.sh, screenshots/ (demo + capture)
benchmarks/tests/<name>/    prompt.md + test.yaml; results go to benchmarks/results/ (gitignored)
logs/  certs/  ui/dist/     Runtime output, the intercept CA plus the terminal access key (certs/remote-access-key), and the UI build; all gitignored
client/bin/ machine/bin/    This machine's builds; dist/<version>/<os>-<arch>/ every platform's (archives and packages, binaries in artifact/); all gitignored
android/                    THE ANDROID APP (Kotlin, no libraries): ServersActivity (servers list), ConsoleActivity (the console in a WebView); see android/README.md
python/agent_py/            THE PYTHON CLIENT AND MACHINE; see python/README.md
  common.py login.py        Shared by both: protocol and headers, credentials files, loopback checks, look_path; login/logout
  client/                   agent: launcher.py (arguments, launch, list, containers), localproxy.py (agent-proxy: ASGI app on uvicorn + HTTPS tunnel port), terminal.py (pty/ConPTY, Share)
  machine/                  agent-machine: __init__.py (command line), presence.py (fleet socket, launches, tunnels), providers.py (model servers, metrics)
```

## Commands

```bash
cd server && pip install -e ".[dev]"     # once; installs agent-server, agent-benchmark
cd ui && npm install                     # once
./scripts/build-client.sh  |  .\scripts\build-client.ps1   # the client into client/bin (--all / -All: the same as build-dist)
./scripts/build-machine.sh |  .\scripts\build-machine.ps1  # agent-machine into machine/bin
./scripts/build-dist.sh    |  .\scripts\build-dist.ps1     # both, every platform, into dist/<version>/<os>-<arch>/: archives (binaries in artifact/); the Python wheel in dist/<version>/python/

cd server && python -m pytest -q         # ~145 tests, ~1 min, no network beyond 127.0.0.1; test_client.py builds the Go client
cd client && go test ./...               # the client's unit tests; go vet ./... and GOOS=linux go vet ./... for both pty paths
cd machine && go test ./...              # agent-machine's unit tests; vet it for Windows and Linux too (process_*.go differ)
cd tools && go test ./...                # tools/dist: package versions, every Linux format for every target, rpm headers
cd python && python -m pytest -q         # the Python agent and agent-machine against a real agent-server (~11 tests, ~1 min)
PACKAGES=1 ./scripts/build-dist.sh       # the same plus .deb/.rpm/.apk/Arch (and .msi with wix, .pkg on a Mac); -Packages in .ps1
cd ui && npm run typecheck               # tsc, strict, noUnusedLocals/Parameters
cd ui && npm run build                   # writes ui/dist

agent-server                             # serves API + ui/dist on 0.0.0.0:9999 (config/backends.yaml); rebuilds ui/dist first if stale
agent-server --reload --no-build         # Python auto-reload, skip the UI build
docker compose up -d                     # the server in Docker (server/Dockerfile)
agent proxy                              # this machine's agent-proxy in the foreground (the launcher otherwise starts it in the background)
./scripts/dev.sh  |  .\scripts\dev.ps1   # API with reload + Vite on :5173 (proxies /api and /health to :9999)
agent --list                             # the server's agents, whether they're installed here, and agent-proxy's server and sign-in
agent-server user add joey --org pockets  # first user = admin; from then on everyone signs in (also: org|team|user)
agent login http://server:9999           # signs this machine's agent-proxy in to a server, once (agent logout)
agent qwen . --model qwen3.6:35b --trace  # launch an agent through agent-proxy (--backend picks one, --control shares its terminal)
```

The one pytest warning (Starlette's httpx deprecation in `TestClient`) is expected.

## How a session works

1. The client's `launcher.EnsureProxy` starts the machine's `agent-proxy` (itself, as `agent proxy`, detached) if `127.0.0.1:8184/health` doesn't answer. It also starts `agent-server` from `PATH` when the proxy's server is loopback and down.
2. The launcher gets the agent from `GET /api/agents` and, for account agents, copies the server's CA and bundle into `~/.cache/agent-proxy/<fingerprint>/`. It then calls `POST /api/agents/{id}/sessions` (`model`, `backend`, `trace`, `project`, `project_path`, sharing, `launcher: true`, its own `proxy_url` and `intercept` paths) on the `agent-proxy`, which forwards it signed with the machine's token. The server picks the backend (`backend_for`), creates the session and returns the rendered `env` and `command`, plus `container` when the backend is a managed container to start first. The console's Agents page uses the same endpoint without the launcher's fields. `POST /api/sessions` remains for other tools.
3. The agent gets URLs on the `agent-proxy` containing the session id, rendered by the server from its `config/agents.yaml` templates:
   - `{openai_url}` = `http://127.0.0.1:8184/session/<id>/v1`
   - `{anthropic_url}` = `…/session/<id>/anthropic`
   - `{ollama_url}` = `…/session/<id>` (native `/api/...`)

   Account agents get `HTTPS_PROXY=http://<id>:agent-proxy@127.0.0.1:8183` and the server's CA, cached in `~/.cache/agent-proxy/<fingerprint>/`. The `agent-proxy` passes the same paths to the server. Bare `/v1`, `/anthropic` and `/api` also work, with the headers `X-AI-Proxy-Session` and `X-AI-Proxy-Backend`.
4. `routes/compat.py` on the server picks the session's backend and translates or forwards. Every request gets a `RequestTelemetry` entry. With `trace`, the full request and response are saved as well.
5. With `--control` (and an interactive terminal), the launcher runs the agent through `terminal.Run` rather than `exec.Cmd.Run`. It shares the terminal at `/api/terminals/<id>/host` on the `agent-proxy`, which relays it to the server.
6. Everything is written to the server's `logs/sessions/<id>/`: `session.json`, `requests.jsonl`, `traces/000001.json`, and `terminal.log`/`terminal.json` when the terminal was shared. The proxy's own log is `logs/proxy/proxy.log`. When the console starts Ollama, its output goes to `logs/ollama/serve.log`.

## Configuration

- `config/backends.yaml`: the server's address (`proxy.url` is also the `agent-proxy`'s server before `agent login`), `public_url`, `default_backend`, the backends (`type`, `url`, `machine`, `container`) and HTTPS capture (`intercept`; `listen_port` only to accept agents directly). **Saving from the console's Settings page (`PUT /api/config`) rewrites this file with `yaml.safe_dump`, so its comments are lost.** Make comment edits by hand, and expect them to go if someone saves from the UI.
- `config/models.yaml`: Ollama-only tuning, applied in layers: `defaults`, then the first matching `profiles` entry (shell pattern on the Ollama model name), then the exact alias under `models`. Values a client sends explicitly win. `Settings.profile()` implements it.
- `config/agents.yaml`: every agent. Supporting a new agent is usually just a new entry (see the comment block at the top of the file for placeholders, `protocol`, `backend_type`, `intercept`, `files`). `files` are rendered by the server and written by the launcher into the `files_dir` it sends (`~/.cache/agent-proxy/sessions/<random>/`, removed when the agent exits); a template whose placeholder has no value for the session is left out (`agents._render`).
- Ollama's `OLLAMA_*` settings are the machine's: agent-machine keeps them in `ollama.json` in its folder and applies them when it starts Ollama. The server has no Ollama config.
- The repo root is found via `AI_PROXY_HOME`, or else the first directory up from the package or the cwd that has `config/backends.yaml`. Environment overrides: `AI_PROXY_HOST`, `AI_PROXY_PORT`, `AI_PROXY_URL`, `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS`, `AI_PROXY_AGENTS`, `AI_PROXY_LOG_DIR`, `AI_PROXY_UI_DIR`, `AI_PROXY_DIST_DIR`, `AI_PROXY_VERSION`, `AI_PROXY_DEFAULT_BACKEND`, `AI_PROXY_INTERCEPT=0`, `AI_PROXY_INTERCEPT_HOST`, `AI_PROXY_INTERCEPT_PORT`, `AI_PROXY_CA_DIR`, `AI_PROXY_LIVE=0` (stop streaming request text to the UI), `AI_PROXY_UI_BUILD=0`. On agent machines: `AI_PROXY_LOCAL_PORT`, `AI_PROXY_LOCAL_INTERCEPT_PORT`, `AI_PROXY_CONTROL=1`, `AI_PROXY_CREDENTIALS`, `AI_PROXY_TOKEN`.
- Secrets never go in `config/`. Strata's key is read by the machine from Strata's own profile and sent in its hello (`api_key` on the provider; the server keeps it in `Machine.keys`, never in machines.json or the console), or comes from `STRATA_API_KEY` on the server. `certs/` holds the intercept CA's private key.

## Backend conventions and gotchas

- **Route order matters.** `routes/compat.py` has catch-alls, including `/api/{path}`, which forwards agents' native Ollama calls. Register every console route before it: `app.py` registers management and the other console routers, then compat, then the UI fallback. Within a router, register literal paths before parameterised ones. For example, `/api/machines/{id}/providers/adopt` comes before `/api/machines/{id}/providers/{provider}`.
- The UI fallback serves `index.html` for any unknown path, except paths starting with `api/`, `v1/`, `session/` or `anthropic/`, which return 404.
- Make upstream HTTP calls with the shared `runtime.client` (an `httpx.AsyncClient`). Don't create your own client: tests replace this one with an `httpx.MockTransport`.
- Calling `Settings(...)` in code leaves HTTPS capture disabled. Even when enabled, the server binds a TCP listener only if `intercept.listen_port` is set (tests use 0), so tests never open port 8183. `Settings.load()` reads the YAML.
- **The client/server boundary is the API.** The client must not read `config/` or import anything of the server's; the server must not assume anything about the client beyond the API. When the client needs something new from the server, add it to the API (and to the agent-proxy's `LauncherAPI` if the launcher calls it), test it in `server/tests/test_client.py`, and keep the server the owner of the logic: agent definitions, backend choice and template rendering live in Python, not Go.
- **The agent-proxy (`client/internal/localproxy`) signs as the user, so treat it like the terminal endpoints.**
  - It binds 127.0.0.1 only and checks for a loopback Host and same-origin on every route (`local`).
  - Only `LauncherAPI` paths are forwarded with the token. Agent traffic (`/session`, `/v1`, `/anthropic`) is streamed without it, because the session id is the credential. Cookies and Origin are never forwarded.
  - Every HTTP client and WebSocket dialer in the client has `Proxy: nil` (`settings.Transport`). Started from inside an intercepted agent, it would otherwise inherit `HTTPS_PROXY` and loop.
  - It reads `agent login`'s credentials file on every request, so `agent login`/`logout` apply at once.
  - Account agents' connections use one WebSocket each to `/api/proxy/tunnel`. There, the server's `InterceptProxy.serve_tunnel` hands one end of a socket pair to the unchanged `_accept`, because `start_tls` needs a real transport.
  - Before answering a refused tunnel with `407`, it reads the agent's `CONNECT` head (`readHead`): on Windows, closing a socket with unread data resets it and the agent never sees the answer. Fake servers in Go tests need the same care (read the hello before closing).
  - `server/tests/test_client.py` runs the built client (and agent-machine, `fleet_machine`) against the server on real ports; follow it for changes that span them.
- Test pattern: each test file has `make_app(tmp_path, handler, ...)` and `close_app(...)`. They build `Settings` in code, run `create_app`, enter a `TestClient`, then swap in a mock transport whose `handler(request)` plays the upstream server. Assert on what the upstream received as well as on what the agent got back. Follow `server/tests/test_model_runner.py` for a new provider.
- Go style in `client/`, `machine/` and `tools/`: `gofmt`, `go vet` clean for Windows and Linux, the standard library first (the only dependencies are gorilla/websocket, x/sys, x/term, and creack/pty in the client; tools/ has none). Each is its own module: never import one from another. Comments explain why, as in the Python. Keep `go.mod` at Go 1.24 and build with `GOTOOLCHAIN=local`.
- Style: `from __future__ import annotations`, a module docstring saying what the module is for, dataclasses and type hints. Comments explain why, not what. No formatter or linter is configured; match the surrounding code, where lines are often long.
- **Terminal endpoints are a remote shell; keep them locked.**
  - Every `/api/terminals*` and `/api/remote/*` route checks `runtime.remote.trusted()` and `remote.same_origin()`.
  - Trusted means the client is loopback **and** the Host header names loopback (DNS rebinding sends the attacker's Host), or the request carries the access key (the `ai_proxy_remote` cookie or the `x-ai-proxy-token` header).
  - Only a local request gets the key back from `GET /api/remote`, or a short pairing code from `POST /api/remote/code`. A code stands in for the key, so it is single-use, expires after `CODE_TTL`, and `CODE_ATTEMPTS` wrong tries cancel it.
- **The access key file.**
  - `Settings(...)` built in code keeps the key under `log_dir`, and `Settings.load()` uses `certs/remote-access-key`.
  - Tests and the demo must never touch the real file. The demo sets `AI_PROXY_REMOTE_KEY_FILE`.
- **Starlette's TestClient sends WebSockets to `ws://testserver`.** Pass `headers={"host": "127.0.0.1:8181"}` and `client=("127.0.0.1", ...)` to look local. See `ws_connect` in `test_terminals.py`.
  - A refused WebSocket left open inside a `with TestClient(...)` block makes the test hang on exit instead of failing; use context managers for sockets.
- **Terminal input details that matter.**
  - Text and Enter go to the agent as separate writes, `InputGap` apart (`client/internal/terminal/share.go`). Claude Code reads text and Enter in one read as a paste.
  - Multi-line text is wrapped in bracketed paste once the agent has enabled it (`\x1b[?2004h`, detected in `terminals.py`).
  - The viewer drops xterm.js's automatic replies to terminal queries (`REPLY` in `Terminal.tsx`); the real terminal on the computer already answers them.
- **Testing terminal hosting for real.**
  - `terminal.Run` needs a real console. On Windows, play the user's terminal by running the built `agent.exe ... --control` inside a `winpty.PTY` (pywinpty, in the server's dev extras), against a server and `agent proxy` on free ports: write keys to it, read its screen, and type from a viewer WebSocket on the server.
  - For the POSIX path, WSL Ubuntu works: build with `GOOS=linux CGO_ENABLED=0` and drive the binary with Python's `pty.fork()` there. Its `/mnt` drives aren't visible from the sandbox, so pipe the files in with `tar cf - . | wsl.exe -d Ubuntu -- sh -c 'cd /tmp/x && tar xf -'` and set `MSYS_NO_PATHCONV=1`. The server's packages aren't installed there, so play the server with a stdlib `http.server`.
  - Never test on ports 8183/8184/9999 on the dev machine: a coding agent working here may itself be running through that agent-proxy and server. Set `AI_PROXY_LOCAL_PORT`, `AI_PROXY_LOCAL_INTERCEPT_PORT`, `AI_PROXY_URL` and `AI_PROXY_CREDENTIALS` (as a Windows path when set from Git Bash) for test runs.
- **Two modes: local and server.** Without accounts, everything works as a single-user tool: this computer is trusted and phones pair with the access key. Once `users.json` has a user, or `AI_PROXY_USERNAME`/`AI_PROXY_PASSWORD` are set, `AuthMiddleware` requires a sign-in for everything under `/api/`, `/v1/`, `/anthropic/`, `/session/` and `/docs`.
  - `app.ensure_first_user` creates an `admin` with a random password when `agent-server` starts listening beyond loopback and `users.json` doesn't exist yet. It prints the password and saves it to `first-user-password` beside `users.json`. It runs in `main()`/`Supervisor.serve`, not `create_app`, so tests stay in local mode.
  - Agent requests are allowed when they name an **open** session, in `/session/<id>/...` or with `X-AI-Proxy-Session` on `/v1`/`/anthropic`. Never replace the agent's own `Authorization`: hosted pass-through needs it.
  - Launchers send `X-AI-Proxy-Token`. Browsers use the `ai_proxy_session` cookie (SameSite=Lax); unsafe methods with another site's `Origin` are refused.
  - Changes to the server itself are admin-only, through the `ADMIN_ONLY` table in `auth.py`. Add new routes like that to it.
  - HTTPS capture answers `407` unless `Proxy-Authorization` names an open session. The tunnel itself needs the machine's token; the `agent-proxy` turns a refused tunnel into a `407` for the agent.
- **Every endpoint that touches a session must check sharing.** Use `access.can_view`/`can_control` (and `is_owner` for changing the sharing), via the `viewable`/`controllable` helpers in `routes/management.py`.
  - Lists filter with them. A session the user can't see answers 404, not 403.
  - Streams (`/api/live`) check per event, so sharing changes apply straight away.
  - Terminal viewers are dropped through `TerminalHub.drop_viewers` when sharing narrows.
  - Clients can't set `owner`/`visibility`/`team`/`access` tags: `access.client_tags` strips them and `access.sharing_tags` validates them.
- **Tests and real files.** `Settings(...)` built in code still points `backends_file` (and the other config files) at the repo's `config/`. Any test that saves config must pass `backends_file=tmp_path / "backends.yaml"`. `tests/conftest.py` fails the test and restores the file otherwise. An early version of `test_auth.py` overwrote the real `backends.yaml` this way.
- **Containers machines run.**
  - A backend with `machine:` and `container:` in backends.yaml is a container on that machine. The machine runs it: the server never runs `docker`. The machine turns it on with `agent-machine containers on`, reports its engines (the table lives in Go, `machine/internal/containers`) in its hello, and the server asks it with `Fleet.ask` over the fleet socket (`{"type": "container", "op": status|start|stop|restart|remove|logs}`, answered by `container_result`).
  - Both sides check specs: `containers.validate_spec()` against the machine's reported engine, and agent-machine again whatever the server sends. They allow only known engines, each engine's own env settings, plain image names, `gpus` of all/none/card numbers, and a volume name or absolute folder. Never add free-form docker arguments: `docker run` is root on that machine.
  - Creating, editing or deleting one needs the machine's owner (and ADMIN_ONLY with accounts); starting and stopping is open to trusted users, as the launcher's `EnsureContainer` needs.
  - `Settings.update_network_config` (the Settings page) keeps existing machine and container blocks and ignores new ones. Only `/api/containers` or a hand edit changes them, via `Settings.save_backend`/`remove_backend`.
  - Strata containers get `PRESET=<name>` and a server-generated `API_KEY`, sent with each request to the machine. The keys live in `container-keys.json` beside the access key and are published to `strata.managed_keys` for `strata.key_for()`.
  - Server tests replace `Fleet.ask` with `FakeMachine` (test_containers.py), or put a `FakeSocket` in the fleet that answers like agent-machine (test_providers.py, provider and server requests); `test_fleet.py` covers the socket round trip and `test_client.py` runs the real agent-machine with `AI_PROXY_CONTAINER_COMMAND='["python", ".../fake_docker.py"]'`. The fake takes a lock-directory, because status polling runs `inspect` while a start or stop is still working.
  - Container jobs run on the machine, so a server restart doesn't wait for or stop them.
  - For a real runtime, Podman works as the machine's CLI (`agent-machine containers cli podman`). The machine here is `podman-machine-default`, normally stopped: start it, test, then clean up and stop it again.
- **Don't write code containing backslash escapes through Bash heredocs.** The tool mangles `\\`, `\n` and `\x1b`. Use the Edit/Write tools, or write the snippet to a file with Write and splice it in.
- **Restarts.** `agent-server` runs under `app.Supervisor`. `POST /api/server/restart` goes to `Runtime.request_restart`, which:
  1. waits until no requests or container actions are in flight (up to 30 s);
  2. ends the live SSE streams, terminal sockets and agent-proxy tunnels;
  3. has the supervisor serve again, in the same process, with `Settings.load()`. That re-reads `config/`, rebuilds the UI if stale and rebinds the address unless `--host`/`--port` pinned it.

  A new kind of long-lived connection must be closed in `_restart_when_idle` too, or it holds up the restart until uvicorn's 5 s `timeout_graceful_shutdown`. There's no supervisor with `--reload`, or when tests build the app directly.
- **The fleet** (`fleet.py`, `routes/machines.py`; `machine/internal/presence`). Each `agent-machine run` holds `WS /api/machines/connect` open, signed with its own token: a hello with the machine's id (from its `machine.json`), installed agents, model servers on loopback ports, whether it runs containers (and its engines), and launch settings; updates every minute.
  - A launch is a remote shell: only the machine's owner may start one (not admins), only when the machine has `launches` on, and only in its `roots`. The server checks the folder (`within_roots`), and the machine checks again after resolving links (`settings.Machine.Within`). Keep both checks.
  - agent-machine runs a launch as `agent <id> <folder> --headless --launch-id <id>` (detached; `agent` from PATH or `AI_PROXY_AGENT`), with `AI_PROXY_URL` set to its server and refused if this computer's agent-proxy already carries traffic to another one. `AI_PROXY_MACHINE_ID`/`_NAME` tag the session. headless mode prints `{"session_id": ...}` as its only line of output, then runs the agent in a pseudo-terminal with no local window (`terminal.RunHeadless`), shared with the server. Sessions carry `machine`, `machine_name` and `launch` tags. Agents started by hand only carry `hostname`; `Fleet.machine_for` puts them under the same owner's machine with that hostname (from its hello), so a machine's page lists both.
  - Adopting a machine's model server (`providers/adopt`) is admin-only (ADMIN_ONLY) and adds a backend at the machine's address as the server saw it, unless the machine names one (`agent-machine address`).
  - Starting the machine's own Ollama and Strata (`POST /api/machines/{id}/servers/{kind}/{action}`) goes over the same socket as `{"type": "server"}`, answered by `server_result` (each answer carries the machine's servers state, which `Fleet.message` keeps). Owner only, like launches, and only with `agent-machine servers on`. `Fleet.ask(..., channel="server")` checks that; `channel="container"` is the containers' request.
  - The fleet's sockets close on a restart (`Fleet.close_all` in `_restart_when_idle`); machines reconnect by themselves.
- **The running proxy doesn't pick up Python changes.** A restart from the console reloads config, not code. Unless it was started with `--reload`, restart `agent-server` itself (port 9999). A rebuilt client needs the running `agent-proxy` (port 8184) stopped, so `agent` starts the new one; a rebuilt agent-machine needs `agent-machine stop` and `start`. UI changes show up after `npm run build` and a page refresh, because the proxy serves `ui/dist` directly.

## UI conventions

- All HTTP calls and their response types live in `ui/src/api.ts`, as `api.<name>` methods or exported helpers. Keep the types in step with the Python response shapes.
- Data comes from TanStack Query. Query keys are short arrays (`["docker"]`, `["docker", name]`, `["models"]`, `["agents"]`). After a mutation, invalidate the affected keys. Common queries are in `hooks.tsx` (`useStatus`, `useModels`, `useAgents`, ...).
- The console has three parts, defined once in `navigation.ts`: Agents (Start an agent, Running agents), Server (Live, Traffic, Requests, Sessions, Server settings, Organizations, Downloads), Machines (All machines, then each machine, listed by App.tsx from `/api/machines`).
  - **Everything about a model server is on its machine's page**, `pages/Machine.tsx` at `/machines/:id/:section`: Overview, one section per model server (`ollama-11434`, from `machineProviders()` in `api.ts`: what the machine reports plus its backends), `ollama` (the installed Ollama's settings), `containers`, `load`. The views are per model server and take a `ProviderRef`: `OllamaProvider` (Models.tsx), `StrataProvider` (Strata.tsx), `DockerProvider` (Docker.tsx), `MachineOllama` (OllamaServer.tsx), `MachineContainers` (Containers.tsx), `MachineLoad` (Hosts.tsx). Don't add global pages for providers; the old addresses (`/models`, `/strata`, ...) redirect to `/machines`.
  - A model server that isn't a backend yet is managed the same way, with **Add as a provider** (admins) at its top. Say so in UI text: local providers come from machines, hosted APIs are the exception (Server settings adds those).
  - **Downloads** (`/get`) is public: App.tsx renders it without the console when signed out, and the sign-in page and the rail link to it. The SPA must not use paths under `/downloads/` or `/android`: the server serves files there.
  - To add a page:
  1. Create `pages/X.tsx`.
  2. Add a `<Route>` in `App.tsx`.
  3. Add an item in `PARTS`.
  4. Put `<PartTag />` above the page's `<h1>`. It's the breadcrumb ("Machines › Ollama").
- Look and layout: Inter, a neutral palette with an indigo accent, cards with a soft shadow (`--shadow`), icons from `components/Icon.tsx` (inline SVG, no icon library). Below 900 px the rail becomes a top bar with a slide-in menu; opening it pushes a history entry so Back (Android's included) closes it. The Android app shows this layout, so check a phone-sized viewport (412 px) for every page.
- Which backends an agent can use, and which one a launch picks, come from the server (`Agent.backends` and `Agent.default_backend` from `/api/agents`, computed by `serves()` and `backend_for()`). Don't re-derive them in the browser (or in the Go client).
- Use existing components before writing new ones:
  - `Tabs`, for tabbed views kept in `?tab=`.
  - `CodeLine`, for copyable commands.
  - `ConfirmDialog`.
  - `ModelSearch`/`usePull(onPulled, provider)`, for streamed pull progress.
  - `AddBackendForm`/`addBackend()`.
  - `ProviderState`/`StateDot`.
  - `LogPanel`.
  - `RouteBadge`.
- Styling goes in `styles.css`, using the existing CSS variables, so both themes keep working. Check dark mode and a narrow viewport (about 420 px) for layout changes.
- UI and README text: plain, short sentences, sentence case, and say what something does for the user. Name providers with `providerLabel()`/`PROVIDERS` in `routes.ts`.

## Checklists

**New agent:** add an entry to `config/agents.yaml` (`executable`, `protocol`, `env`/`args` templates, `install`, `homepage`, and `backend_type` or `intercept`). The server reads it on demand and renders it for every launch, so the client needs no change. Then:

- Run `agent --list` to check it's found.
- Launch it once with `--trace` and read the recorded traces.
- Add it to the README's "Supported agents" section.

**New backend/provider type**, the places Docker Model Runner touched:

1. `config.py`: `BACKEND_TYPES`, `NATIVE_PROTOCOLS` (if it's pass-through), `PROVIDER_NAMES`; `fleet.PROVIDER_TYPES` if machines run it.
2. `routes/compat.py`: `MESSAGES_BACKENDS`, path mapping in `_forward`, and `_list_models` (agents' traffic only).
3. agent-machine: a kind in `machine/internal/providers` (status, model ops, log), its probe in `presence.DetectProviders`, and its Go tests.
4. `routes/management.py`: `_backend_models` shapes the machine's `status` for `/api/models`. The console's provider routes in `routes/machines.py` are generic; nothing per provider goes in the server.
5. UI:
   - `api.ts` types and methods.
   - `routes.ts` `PROVIDERS`.
   - `Settings.tsx`: the type `<option>` and `URL_PLACEHOLDERS`; `MACHINE_TYPES` in `api.ts` and `config.py` if it runs on machines.
   - A `<Kind>Provider` view taking a `ProviderRef`, and its case in `ProviderView` in `pages/Machine.tsx`.
6. `config/backends.yaml` and `config/agents.yaml` comments, the README (setup, console, configuration and API sections), and the fake server in `scripts/screenshots/demo.py`.
7. Tests: traffic in `server/tests/test_<provider>.py`, the relay in `test_providers.py`, the machine's side in Go.

## Checking changes visually

`python scripts/screenshots/demo.py` runs a self-contained demo:

- The server is on :8199, with a direct intercept listener on :18199 (`intercept.listen_port`); there's no agent-proxy in the demo.
- Three made-up shared terminals: Claude Code waiting at a permission prompt, OpenCode working, Codex finished. They're fed through `FakeLauncher` in `demo.py`, which speaks the launcher's side of `/api/terminals/<id>/host`, and typing into them echoes back.
- Three made-up machines (`ai-desktop`, `ai-laptop`, `build-box`) joined over the fleet socket. `FakeProviders` answers their provider requests the way agent-machine does, from the fake model servers on loopback, with made-up metrics and catalogue results. The two model machines own the fake backends and run three containers, answered in memory by `FakeContainers`: `strata-iq3` ready, `strata-coder` waiting to switch in, `ollama-cpu` on the fake laptop Ollama. build-box takes launches and has an Ollama not added yet.
- Fake backends: Ollama "laptop" on :18434, Ollama "desktop" on :18435, Anthropic on :18436 and Docker Model Runner on :18437.
- Seeded account sessions and live traffic.

It uses a temporary home, so your real `logs/` and `config/` aren't touched. Stop it with Ctrl+C.

`node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots [name,...]` regenerates the README screenshots. It needs `playwright-core` and a browser. If Playwright's Chromium isn't installed, pass an installed browser with `chromium.launch({ executablePath })`. On this machine, Edge is at `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`. Keep throwaway screenshot scripts outside the repo.

## Environment

- Development is on Windows 11, with PowerShell 5.1 and Git Bash both available. Scripts come in `.ps1` and `.sh` pairs; change both.
- Python 3.13 with no virtualenv; the server package is installed editable from `server/`. Go 1.25 is installed; `GOTOOLCHAIN=local` keeps it from downloading another.
- `go get` fails TLS checks inside an intercepted agent session (Go on Windows ignores `SSL_CERT_FILE`); run Go module downloads with `HTTPS_PROXY`/`HTTP_PROXY` unset.
- Podman (`podman-machine-default`, WSL-based, no localhost forwarding) builds and runs `server/Dockerfile`; reach a published port at the VM's address (`ip -4 addr` inside `wsl -d podman-machine-default`). Pass `--http-proxy=false` to `podman build`.
- `core.autocrlf=true` and working copies use CRLF. Keep a file's existing line endings.
- Ports in normal use:
  - 9999: agent-server, on all interfaces (`config/backends.yaml`; the code's default without a config is 8181).
  - 8184: agent-proxy, loopback only (its launcher API and agents' model endpoints).
  - 8183: agent-proxy's HTTPS proxy for account agents, loopback only. The server itself no longer listens there.
  - 5173: Vite. It proxies `/api` with `ws: true` and forwards the browser's Host, so the dev server counts as local.
  - 11434: Ollama.
  - 18500: Strata (from `config/backends.yaml`).
  - 12434: Docker Model Runner.
- Docker isn't installed on the main dev machine. The Docker Model Runner support is built from Docker's API docs and the docker/model-runner source, and is tested against mocks and the demo's fake server, not a real Model Runner.
- **Testing the installers and packages.** WSL has Ubuntu, `FedoraLinux-44` and `archlinux`; for Alpine, `wsl --import` its minirootfs. `wsl -d <distro> -u root` is root without a password, so install the built packages with each distro's own tool (`dpkg -i`, `dnf install`, `apk add --allow-untrusted`, `pacman -U`) and remove them again. Use `wsl --exec sh -c '...'`: plain `wsl -- ...` runs the line through another shell first, which expands `$?` too early.
  - The Ubuntu user runs a real agent-machine signed in to the real server. `install.sh --service` stops it (that's its job) and the test service then joins the real fleet. Test services as a throwaway user in another distro (`useradd -m aptest`, then `SUDO_USER=aptest ./install.sh --service` as root), where nothing is signed in.
- Commit messages are a short one-line summary. Don't commit `ui/dist`, `logs/`, `certs/` or benchmark results.
