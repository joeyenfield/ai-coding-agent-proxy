# Development

A guide for changing this repository. [`AGENTS.md`](../AGENTS.md) at the root is the detailed map of the code and its rules; read it before a change that crosses components. [Architecture](architecture.md) explains how the parts fit together.

## Layout

The components live in a folder each and talk only over HTTP and WebSockets, never by importing each other.

```text
server/          The server, in Python (FastAPI): agent-server and agent-benchmark, its Dockerfile
  agent_server/  The package: app.py, config.py, runtime.py, auth.py, fleet.py, routes/, translate/, ...
  tests/         pytest, one file per area; test_client.py runs the real Go client against it
ui/              The console: React 19, TypeScript, Vite, TanStack Query; the server serves its build
client/          The client, in Go: agent (launcher, agent login, update) and agent-proxy
machine/         agent-machine, in Go, its own module: the fleet connection, providers, containers, servers
python/          agent_py: the same agent, agent-proxy and agent-machine in Python
android/         The Android app (Kotlin): a list of servers, each one's console in a WebView
tools/dist/      Build tooling, in Go: every platform's binaries, archives, packages and the Python wheel
config/          backends.yaml, models.yaml, agents.yaml
scripts/         Build, dev and launcher scripts (.ps1 and .sh pairs), HTTPS setup, screenshots
sandbox/         The agent-sandbox image
benchmarks/      tests/<name>/ (prompt.md, test.yaml); results/ is gitignored
VERSION          The project's one hand-written version
```

`logs/`, `certs/`, `ui/dist/`, `client/bin/`, `machine/bin/` and `dist/` are runtime output and builds, all gitignored. Don't commit them, or benchmark results.

## Setup

You need Python 3.11 or later (development uses 3.13), Node with npm, and Go (the modules stay at Go 1.24; build with `GOTOOLCHAIN=local` so Go doesn't download another toolchain).

```bash
cd server && pip install -e ".[dev]"    # agent-server and agent-benchmark, plus pytest and friends
cd ui && npm install
./scripts/build-client.sh               # agent and agent-proxy into client/bin
./scripts/build-machine.sh              # agent-machine into machine/bin
```

On Windows, every script has a PowerShell twin: `.\scripts\build-client.ps1` and so on. When you change one, change both.

## Running it

```bash
agent-server                       # API and console on the configured address; rebuilds ui/dist if stale
agent-server --reload --no-build   # Python auto-reload, skip the UI build
./scripts/dev.sh                   # agent-server --reload --no-build plus Vite on :5173, with hot reload
./scripts/agent.sh qwen . --model qwen3.6:35b   # build the client from this checkout and launch an agent
agent proxy                        # this machine's agent-proxy in the foreground
```

Vite on 5173 proxies `/api` (WebSockets included) and `/health` to the server on 9999, and forwards the browser's `Host`, so the dev server counts as this computer.

**What picks up a change:**

- Python: restart `agent-server`, unless it runs with `--reload`. The console's restart reloads `config/`, not code.
- The console: `npm run build`, then refresh the page. The server serves `ui/dist` directly.
- The client: stop the running `agent-proxy` (port 8184), so the next `agent` starts the new one.
- agent-machine: `agent-machine stop`, then `agent-machine start`.

**Don't test on 8183, 8184 or 9999 on a computer whose coding agent may itself be running through them.** Set `AI_PROXY_LOCAL_PORT`, `AI_PROXY_LOCAL_INTERCEPT_PORT`, `AI_PROXY_URL` and `AI_PROXY_CREDENTIALS` for test runs.

## Tests and checks

| Command | What it checks |
|---|---|
| `cd server && python -m pytest -q` | The server: every area, against mocked upstreams. `test_client.py` builds `../client` and `../machine` and runs them against a real server on free ports (skipped without Go). No network beyond 127.0.0.1. |
| `cd python && python -m pytest -q` | The Python `agent` and `agent-machine` against a real server, including `server/tests/test_client.py`'s contract. |
| `cd client && go test ./...` | The client's unit tests. |
| `cd client && go vet ./... && GOOS=linux go vet ./...` | Both pseudo-terminal paths (ConPTY and POSIX). |
| `cd machine && go test ./...` | agent-machine's unit tests. Vet it for Windows and Linux too. |
| `cd tools && go test ./...` | tools/dist: package versions, every Linux format for every target, rpm headers. |
| `cd ui && npm run typecheck` | `tsc`, strict, with `noUnusedLocals` and `noUnusedParameters`. |
| `cd ui && npm run build` | Writes `ui/dist`. |

The one pytest warning, Starlette's httpx deprecation in `TestClient`, is expected.

`server/tests/conftest.py` fails any test that changes the real `config/*.yaml` or creates secrets in `certs/`, and restores the file. A test that saves config must pass `backends_file=tmp_path / "backends.yaml"` to `Settings(...)`.

Each server test file has `make_app(tmp_path, handler, ...)` and `close_app(...)`: they build `Settings` in code, run `create_app`, and swap the shared HTTP client's transport for a mock whose `handler(request)` plays the upstream. Assert on what the upstream received as well as what the agent got back. `fake_docker.py`, `fake_ollama.py`, `fake_strata.py`, `fake_mcp.py` and `fake_systemctl.py` stand in for real programs.

## The demo and screenshots

```bash
python scripts/screenshots/demo.py                                    # leave it running
node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots [name,...]
```

`demo.py` runs a self-contained server on port 8199 in a temporary home, so your `logs/`, `config/`, accounts and access key aren't touched. It has fake Ollama, Anthropic and Docker Model Runner servers, three made-up machines joined over the fleet socket (with fake model servers, metrics, catalogue results and containers), three made-up shared terminals you can type into, seeded account sessions, and live traffic. `scripts/screenshots/traffic.py` drives agent-like traffic through a server for smoke tests. Stop the demo with Ctrl+C.

`capture.mjs` needs `playwright-core` and a browser (`npx playwright-core install chromium-headless-shell`, or point `chromium.launch({ executablePath })` at an installed one). Keep throwaway screenshot scripts outside the repository.

Check layout changes in dark mode and at a phone-sized width (about 412 px): the Android app shows the same pages.

### Diagrams in the docs

Draw diagrams in `docs/` as [Mermaid](https://mermaid.js.org) in a ` ```mermaid ` block: `sequenceDiagram` for a flow between parts (a session, a launch, the MCP relay), `flowchart` for how parts connect. The console's **Docs** page (`/help`) draws them with mermaid.js, loaded only on pages that have one, in its light or dark theme, and shows the source with the error if one can't be drawn. GitHub draws the same blocks, so the files read the same in the repository. Write `<id>` as it is in a sequence diagram, and as `#lt;id#gt;` in a flowchart label (where `<...>` would be read as HTML). Check a new diagram on the Docs page in both themes.

### Playwright MCP for coding agents

The repository's `.mcp.json` gives coding agents working on it (Claude Code, and others that read the file) the [Playwright MCP server](https://github.com/microsoft/playwright-mcp): a browser they can drive to open the demo or a real console, click through pages, read them and take screenshots. It runs `npx -y @playwright/mcp@latest --browser msedge`, so it uses the installed Microsoft Edge rather than a downloaded browser; change `--browser` to `chrome` or `chromium` elsewhere. Claude Code asks you to approve a project's MCP servers the first time it starts in the folder (`claude mcp list` shows it as pending until then).

The browser doesn't use the agent's `HTTPS_PROXY`, so the pages it opens aren't recorded by the server (only its MCP calls are, when a machine shares it; see [MCP servers](mcp.md)). Add `--ignore-https-errors` for a server with a self-signed certificate.

## Versions and the protocol

`VERSION` holds `X.Y.Z` and is the only version written by hand. A build is `X.Y.Z` when `HEAD` is tagged `vX.Y.Z` and clean, otherwise `X.Y.Z+<commit>`, with `.dirty` for uncommitted changes to tracked files outside `config/` (the console saves settings there). `server/agent_server/version.py` and `tools/dist/version.go` compute it the same way; keep them in step. `AI_PROXY_VERSION` overrides both.

Compatibility is `PROTOCOL`, an integer in `version.py` and in `settings.Protocol` in `client/` and `machine/`; `test_client.py` checks they agree.

- Raise `PROTOCOL` everywhere for a change older programs can't follow.
- Raise `MIN_PROTOCOL` only when the server stops supporting an older one.
- A change to the API between the client or machine and the server goes into `python/` too.

To release, change `VERSION`, commit, tag `vX.Y.Z`, and build the distributions.

## Distributions

`scripts/build-dist.sh` (`.\scripts\build-dist.ps1`) runs `tools/dist` (`go run ./dist` in `tools/`), which builds both Go components for every target with `CGO_ENABLED=0`, packs them, and builds the Android app and the Python wheel.

| System | Folder names |
|---|---|
| Windows | `windows-amd64`, `windows-arm64`, `windows-386` |
| Linux | `linux-amd64`, `linux-arm64`, `linux-armv7`, `linux-386`, `linux-ppc64le`, `linux-riscv64`, `linux-s390x`, `linux-loong64` |
| macOS | `darwin-amd64`, `darwin-arm64` |
| FreeBSD | `freebsd-amd64`, `freebsd-arm64` |

```text
dist/<version>/
  SHA256SUMS                                        over every file below
  <os>-<arch>/
    agent-<version>-<os>-<arch>.tar.gz              agent and agent-proxy, with install.sh (.zip and install.ps1 on Windows)
    agent-machine-<version>-<os>-<arch>.tar.gz      agent-machine alone, for machines that only supply models
    *.deb, *.rpm, *.apk, *.pkg.tar.gz, *.msi, *.pkg with -packages
    artifact/                                       the binaries and the installer script; agent update takes these
  android/agent-proxy-<version>.apk                 when a JDK and the Android SDK are found
  python/ai_proxy_agent_py-<version>-py3-none-any.whl
```

| `build-dist.sh` | `build-dist.ps1` | What it does |
|---|---|---|
| `linux/arm64 windows/amd64` | `-Targets linux/arm64,windows/amd64` | Only these targets. |
| `ONLY=agent-machine` | `-Only agent-machine` | Only one component. |
| `PACKAGES=1` | `-Packages` | Native packages too. |
| `ANDROID=0` | `-NoAndroid` | Without the Android app. |
| `PYTHON=0` | `-NoPython` | Without the Python wheel. |
| `VERSION=...` | `$env:VERSION` | Name another version. |

A build replaces its version's folder and leaves the others alone. `build-client --all` (`-All`) is the same as `build-dist`.

The Linux packages (`.deb`, `.rpm`, `.apk`, Arch) are written in Go by `tools/dist`, so they come out the same from any build machine. `.msi` needs [WiX](https://wixtoolset.org) 4 or later on `PATH`, and macOS `.pkg` needs `pkgbuild` (a Mac); a missing tool skips its format with a note. Package versions are the build's `X.Y.Z`, with the commits since the tag as the release. None are signed.

To test installers and packages, use a throwaway user in a separate WSL distribution, not your own user: a test service would join the real fleet. `AGENTS.md` has the details.

## Benchmarks

Add a folder under `benchmarks/tests/` with `prompt.md` and `test.yaml`, then:

```bash
agent-benchmark reasoning --model qwen3.6:35b --backend desktop
agent-benchmark --all
```

Each run gets its own session, and results go to `benchmarks/results/<timestamp>/`. The keys of `test.yaml` and the flags are on [Configuration](configuration.md#agent-benchmark).

## Checklists

**A new agent.** Add an entry to `config/agents.yaml`: `executable`, `protocol`, `env` and `args` templates, `install`, `homepage`, and `backend_type` or `intercept` (see [Agents](agents.md)). The server renders it for every launch, so the client needs no change. Then:

- run `agent --list` to check it's found;
- launch it once with `--trace` and read the recorded traces;
- add it to the supported agents in [Agents](agents.md).

**A new backend or provider type.** The places Docker Model Runner touched:

1. `config.py`: `BACKEND_TYPES`, `NATIVE_PROTOCOLS` (if it's passed through), `PROVIDER_NAMES`, and `MACHINE_TYPES` if machines run it; `fleet.PROVIDER_TYPES` too.
2. `routes/compat.py`: `MESSAGES_BACKENDS`, the path mapping in `_forward`, and `_list_models`. Agents' traffic only.
3. agent-machine: a kind in `machine/internal/providers` (status, model operations, log), its probe in `presence.DetectProviders`, and Go tests.
4. `routes/management.py`: `_backend_models` shapes the machine's status for `/api/models`. The console's provider routes in `routes/machines.py` are generic.
5. The console: types and methods in `api.ts` (and its `MACHINE_TYPES`), `PROVIDERS` in `routes.ts`, the type option and URL placeholder in `Settings.tsx`, and a `<Kind>Provider` view taking a `ProviderRef`, with its case in `ProviderView` in `pages/Machine.tsx`.
6. The comments in `config/backends.yaml` and `config/agents.yaml`, these docs, and the fake server in `scripts/screenshots/demo.py`.
7. Tests: its traffic in `server/tests/test_<provider>.py` (follow `test_model_runner.py`), the relay in `test_providers.py`, the machine's side in Go.

**A new console page.** Create `ui/src/pages/X.tsx`, add a `<Route>` in `App.tsx` and an item in `PARTS` in `navigation.ts`, and put `<PartTag />` above its `<h1>`. Anything about a model server goes on its machine's page instead.

## Conventions worth knowing

- **Route order matters.** `routes/compat.py` has catch-alls, including `/api/{path}` for agents' native Ollama calls. `app.py` registers every console router before compat, then the console's fallback. Within a router, register literal paths before parameterised ones.
- **One HTTP client.** Make upstream calls with the shared `runtime.client`. Tests replace its transport with a mock; a client of your own would bypass that.
- **The server never manages a model server.** It only forwards agents' traffic. Everything else is a request to the machine that runs it, over the fleet socket. Add a provider operation to agent-machine and relay it.
- **Upstream calls to a machine's model server use `runtime.tunnels.base_url(backend)`,** not `backend.url`, so they go through the machine's tunnel.
- **The client/server boundary is the API.** The client never reads `config/` or imports the server; the server owns agent definitions, backend choice and template rendering. Something new the client needs goes into the API, the `agent-proxy`'s `LauncherAPI` allowlist if the launcher calls it, and `test_client.py`.
- **Tests never touch real files.** Pass a temporary `backends_file`; `Settings(...)` built in code keeps the access key under `log_dir` and leaves HTTPS capture off.
- **Admin-only routes go in `auth.ADMIN_ONLY`.** Every route that touches a session checks its sharing; every machine route checks its feature with `fleet.allowed`.
- **Long-lived connections close on a restart.** A new kind must be closed in `Runtime._restart_when_idle`, or it holds the restart up.
- **`PUT /api/config` drops comments** from `backends.yaml`. Make comment edits by hand.
- **Style.** Python: `from __future__ import annotations`, a module docstring, dataclasses and type hints, comments that explain why. Go: `gofmt`, `go vet` clean for Windows and Linux, the standard library first. UI: calls and types in `api.ts`, data from TanStack Query, styles in `styles.css` with its variables. UI and docs text: plain, short sentences in sentence case.
- **Don't write backslash escapes through shell heredocs**; the tooling mangles them. Use an editor.
- **Line endings.** Working copies use CRLF (`core.autocrlf=true`); keep a file's existing endings.
- **Commit messages** are a short one-line summary.
