# Working on this project

Notes for coding agents (and people) changing this repository. `README.md` is the short introduction and `docs/` (start at `docs/index.md`) the user documentation. This file is the entry point for changing the code: the commands, the rules every change follows, and the dev machine. How each component is built lives in `.context/`. Design write-ups go in `docs/`, in the technical overview section (`architecture.md`, `flows.md`, the `*-internals.md` pages), not in separate design or proposal folders.

## Read `.context/` before you change a component

`.context/` holds the design of each component, one file each: how it is built, what it owns, what it must never do, where a change goes, how it is tested, and the checklists. **Before changing a component, read `.context/overview.md` and that component's file.** Don't guess from this file; the detail is there.

| Working in | Read |
| --- | --- |
| Anything across components, or new here | `.context/overview.md` (roles, routes to a provider, a launch end to end, where state lives, ports) |
| `server/` | `.context/server.md` (modules, flows, configuration and environment variables, rules for changes, several processes, tests, checklists for a new route, backend type, agent, setting or table) |
| `client/` | `.context/client.md` (launcher, agent-proxy, tokens, testing terminal hosting) |
| `machine/` | `.context/machine.md` (fleet protocol, toggles, providers, containers, MCP bridge) |
| `python/` | `.context/python.md`, plus `client.md` or `machine.md`, which it mirrors |
| `ui/` | `.context/console.md` (pages, data layer, Machine page, checking visually, writing `docs/` pages). `.mcp.json` gives coding agents a browser (the Playwright MCP server) to open the demo and check pages; its traffic doesn't go through the agent's proxy, so the server doesn't record it (`docs/development.md`) |
| `android/` | `.context/android.md` |
| `tools/`, `devhelpers/` (scripts, from-source stacks), Docker, `deploy/` (published-image Compose and Helm), `sandbox/`, `site/` | `.context/build-and-deploy.md` (versions, dist, installers and testing them, images, Compose, deploy) |
| Sign-in, tokens, roles, tenants, sharing, machine grants, or any new route | `.context/auth.md` |

`.context/README.md` is the index of these files, with what each covers.

When a change alters a component's shape (a module, message type, boundary, file on disk or invariant), update its `.context/` file in the same change. Where `.context/` or this file disagree with the code, the code wins: fix the document.

## Every change: review the docs

Reviewing the documentation is a step of every change. Before calling a change done, check what it touched and update, in the same change:

- `docs/`: any command, option, console page or control, endpoint (`docs/api.md`), setting, file on disk or environment variable added, changed, renamed or removed, and changes in behaviour. `docs/index.md` says which page covers what.
- `.context/`: the component's file, when its shape changed.
- This file, when the layout, commands or rules below no longer match. `docs/development.md` repeats the layout, the setup and test commands and many of the rules for people: change both, or they drift.
- `README.md`, only if the introduction or its links are now wrong.
- Comments in `server/agent_server/defaults.py` (shipped agents and model tuning), when a setting's meaning changed.
- Screenshots in `docs/screenshots/`, when a page they show changed visibly (`.context/console.md`, Checking changes visually).

If nothing needed updating, say so when reporting the change.

## The system in brief

A proxy that every coding-agent request goes through. Components live in their own folders and talk **only over HTTP and WebSockets, never by importing each other**:

- **Server** (`server/`, Python, `agent-server`): the hub. API, accounts, recording, routing to providers; owns all configuration and agent definitions, in one database (PostgreSQL in production, SQLite by default). `agent-auth` (`issuer.py`) is its separate OIDC issuer. Servers can link with each other (`links.py`, `bridges.py`: peering) and only ever talk to each other server to server.
- **Console** (`ui/`, React): only calls the server's API; the server serves its build (`ui/dist`).
- **Client** (`client/`, Go, `agent`): the launcher, `agent login`, and the loopback `agent-proxy` that carries agents' traffic to the server.
- **Machine** (`machine/`, Go, its own module, `agent-machine`): keeps a computer in the server's fleet and runs every local model server, container and shared MCP server there.
- **Python client and machine** (`python/`, `agent_py`): drop-in versions of `agent`, `agent-proxy` and `agent-machine`.
- **Android app** (`android/`): the console in a WebView.

The rules that shape everything (each explained in `.context/overview.md`):

- **The server owns the logic.** Agent definitions, template rendering and backend choice (`serves()`, `backend_for()`) are Python; the client, console and machines take them from the API and never re-derive them.
- **The server never manages a model server.** It only forwards agents' traffic. Listing, pulling, status, logs, settings and start/stop are fleet requests to the machine that runs it. Local backends come only from machines. The exception is the hosted APIs and gateways the server calls itself (backend types `anthropic` and `api`, `api_providers.py`: OpenAI, Anthropic, DeepSeek, OpenRouter and the like).
- **Every upstream call to a machine's model server uses `runtime.tunnels.base_url(backend)`**, never `backend.url`.
- **The Python programs do everything the Go ones do.** A change to `client/` or `machine/` is made in `python/` in the same change, with mirroring tests. The Go code is the reference. Any API change between the client or machine and the server is made in Go and Python and covered by `server/tests/test_client.py`, which runs the Go programs; `python/tests/test_contract.py` runs the same tests against the Python ones, so both must pass. The one Python-only program is `agent-tray` (`python/agent_py/tray.py`), a desktop helper that only runs the other programs' commands.
- **Authentication is mandatory** (OIDC, protocol 2): no loopback trust, no access key, no anonymous mode. **Tenants come first**: check the tenant before roles, sharing or grants. Every new route goes into `ADMIN_ONLY`/`SITE_ADMIN_ONLY` as needed and checks sharing or machine features (`.context/auth.md`).
- **One version, one protocol.** `VERSION` is the one hand-written version; raise `PROTOCOL` for a change older programs can't follow (`.context/build-and-deploy.md`). Build snapshots locally with `devhelpers/scripts/release.ps1|.sh website|services|installers`; no Actions runner is needed. To release a stable version: change `VERSION`, commit, tag `vX.Y.Z`, then build with `--stable` and publish explicitly (`docs/releases.md`). Tags do not start builds. A version commit and a pushed tag are hard to undo: only release when asked.

## Layout

```text
server/agent_server/   the server package (FastAPI): routes/, db/, translate/; .context/server.md has the module map
server/tests/          pytest, one file per area; test_client.py builds the Go client and machine and runs them against a real server
client/                agent and agent-proxy (Go module); cmd/, internal/
machine/               agent-machine (Go module); cmd/, internal/
python/agent_py/       the Python agent, agent-proxy and agent-machine; python/tests/test_contract.py runs test_client.py against them
ui/src/                the console: api.ts, navigation.ts, pages/, components/, styles.css
android/               the Android app
tools/dist/            builds every platform's archives, packages, the wheel and the APK (Go module)
deploy/                runs the PUBLISHED images only (deploy/README.md):
  compose/             quickstart/ (server, issuer, two machines, Ollama; its start.py and machine.py are shared), allinone/ (the
                       ai-control-mesh image: the whole system in one container), peer/ (two linked servers), server/ (for real use)
  helm/                agent-mesh/ (the Kubernetes chart), quickstart/ (its values and a machine on a local cluster)
devhelpers/            for development (devhelpers/README.md): testdata/ (three organizations with people, machines with Ollama
                       and a toolbox MCP server inside each; console on :49999, issuer on :50000), basic/, allinone/, peer/
                       (the deploy/compose stacks built from this checkout; allinone/ has the image's Dockerfile), server/
                       (an override that builds deploy/compose/server), scripts/ (build, dev, screenshot, site and release
                       scripts, .sh/.ps1 pairs; the deploy scripts there are untracked, see Rules)
benchmarks/            agent-benchmark's tests (tests/<name>/prompt.md and test.yaml); results/ is gitignored
sandbox/  site/        the sandbox image, the brochure site
VERSION                the one hand-written version
docs/                  user documentation, also served by the console at /help
.context/              the design of each component (read it first)
logs/ certs/ dist/ ui/dist/ client/bin/ machine/bin/   runtime output and builds, all gitignored
```

## Commands

```bash
cd server && pip install -e ".[dev]"     # once; installs agent-server, agent-auth, agent-benchmark
cd ui && npm install                     # once
./devhelpers/scripts/build-client.sh  | .\devhelpers\scripts\build-client.ps1    # the client into client/bin
./devhelpers/scripts/build-machine.sh | .\devhelpers\scripts\build-machine.ps1   # agent-machine into machine/bin
./devhelpers/scripts/release.sh website  # or services / installers; --publish uploads directly, --plan previews (PowerShell: release.ps1)
./devhelpers/scripts/build-dist.sh    | .\devhelpers\scripts\build-dist.ps1      # every platform into dist/<version>/ (PACKAGES=1 / -Packages adds .deb/.rpm/.apk/Arch)

cd server && python -m pytest -q         # server tests, a SQLite file per test; test_client.py builds the Go programs
podman run -d --name agent-test-postgres -e POSTGRES_PASSWORD=testpass -p 55432:5432 postgres:17
AI_PROXY_TEST_DATABASE_URL=postgresql+psycopg://postgres:testpass@127.0.0.1:55432/postgres python -m pytest -q   # the same on PostgreSQL
cd client && go test ./...               # plus go vet ./... and GOOS=linux go vet ./...
cd machine && go test ./...              # vet for Windows and Linux too
cd tools && go test ./...
cd python && python -m pytest -q         # the Python programs against a real agent-server
python -m pytest -q devhelpers/basic/tests     # end to end against the running basic stack (skipped if it's down; QUICKSTART_E2E=1 fails instead)
cd ui && npm run typecheck && npm run build
helm lint --strict deploy/helm/agent-mesh -f deploy/helm/agent-mesh/ci/<file>.yaml   # and helm template | kubeconform -strict (see Environment)
agent-benchmark reasoning --model <model> --backend <backend>   # a benchmark from benchmarks/tests (--all runs every one)

agent-server                             # API + ui/dist on 0.0.0.0:9999; rebuilds ui/dist first if stale
agent-server --reload --no-build         # Python auto-reload, skip the UI build
agent-server downloads fetch             # a release's agent and agent-machine builds into dist/, for the server to hand out (no Go needed)
agent-server local [start|stop|status]   # a whole instance for one person on loopback 19999/20000 (devhelpers/scripts/install-local.sh|.ps1 installs and starts it)
python devhelpers/scripts/release/release.py --version X.Y.Z --base-url <url>   # package already-built release files and web installers (no Actions)
agent-tray [--status] [--autostart on|off]   # the tray icon (pip install "./python[tray]"); --status prints what it shows
podman build --http-proxy=false -f devhelpers/allinone/Dockerfile --build-arg AGENTS= -t ai-control-mesh .   # the all-in-one image (AGENTS= skips the npm agents)
agent-server config export|import        # every setting as one YAML document
agent-server db info|migrate|import      # the database
agent-server user add <name> --org <org> # accounts (also user passwd, org, team)
agent-server vault key|status|rotate  ·  agent-server audit verify|checkpoint|key   # stored-token keys, the audit chain (docs/configuration.md has the rest)
agent-auth                               # the OIDC issuer on 127.0.0.1:10000
./devhelpers/scripts/dev.sh | .\devhelpers\scripts\dev.ps1     # API with reload + Vite on :5173
python devhelpers/scripts/screenshots/demo.py       # self-contained demo on :8199 for visual checks (--capture [names] takes screenshots and stops; --stop stops a leftover)
node devhelpers/scripts/screenshots/capture.mjs <url> docs/screenshots [names]   # screenshots of any running console
cd deploy/compose/server && docker compose up -d   # PostgreSQL, agent-auth and the server from ghcr.io (needs Docker: not on this machine)
cd deploy/compose/quickstart && podman compose up -d   # the quickstart, from the published images (deploy/README.md)
cd devhelpers/basic && podman compose up -d --build     # the same stack built from this checkout, to try changes (devhelpers/README.md)
cd devhelpers/testdata && podman compose up -d --build  # test data: three organizations, people, a machine with Ollama and an MCP server each (console :49999, issuer :50000)
cd deploy/compose/peer && podman compose up -d && python3 setup.py   # two linked servers, bridged (peering: docs/peering.md)
agent login <url>  ·  agent --list  ·  agent qwen . --model <model> --trace [--control]  ·  agent proxy (the agent-proxy in the foreground)
./devhelpers/scripts/agent.sh qwen . --model <model> | .\devhelpers\scripts\agent.ps1 ...   # build the client from this checkout and launch
agent-machine run [--server <url>]       # keep this computer in the fleet, in the foreground (start|stop|status run it as a service)
```

## Rules every change follows

- **Route order matters.** `routes/compat.py` has catch-alls (`/api/{path}`, `/v1/{path}`); every console route registers before it, and literal paths before parameterised ones.
- **Database**: SQLAlchemy Core only, so the same query runs on PostgreSQL and SQLite; nothing outside `server/agent_server/db/` writes dialect-specific SQL (locks too: `Database.hold_lock`, `Database.transaction_lock`). A schema change is a new step appended to `db/migrate.py`, never an edit to an existing step. Every test passes on both databases.
- **Upstream HTTP** uses the shared `runtime.client`, which keeps no cookies; never create your own (tests swap it for a mock transport).
- **Secrets** never go in the settings (nothing `config export` writes is secret), in Redis or messages in the clear, or in logs and events.
- **Anything that can run commands** (launches, shells, terminals, containers) is checked by the server and again by the machine. Keep both checks. Never add free-form docker arguments.
- **A new long-lived connection** must be closed on restart (`_restart_when_idle`) and renew its token in place if a client program holds it open. **A new per-process state** must say how another process finds it (`.context/server.md`, Several processes).
- **The agent-proxy signs as its user**: loopback only, Host and Origin checked, `Proxy: nil` on every HTTP client and dialer.
- **Tests**: follow the component's pattern in its `.context/` file. Never touch the real `logs/agent-server.sqlite3` or `certs/` (`conftest.py` fails a test that does).
- **Style**: Python with `from __future__ import annotations`, a module docstring, dataclasses and type hints; Go `gofmt`, standard library first, `go.mod` at Go 1.24, built with `GOTOOLCHAIN=local`; comments explain why, not what. UI, README and docs text: plain short sentences, sentence case.
- **`deploy/` runs only published images; `devhelpers/` builds from source.** A Compose stack goes in `deploy/compose/` with the `ghcr.io` images at `${AI_PROXY_IMAGE_TAG:-latest}`, and its from-source twin in `devhelpers/` (the same file with `build:` and a `-dev` project name; `server/tests/test_quickstart.py` keeps them in step). Developer scripts live in `devhelpers/scripts/`.
- **Scripts** come in `.ps1` and `.sh` pairs; change both (except `devhelpers/scripts/start-ollama.sh` and `devhelpers/scripts/https/setup-https.ps1`). The deploy scripts (`devhelpers/scripts/deploy.sh|.ps1`, `deploy-remote.sh`, `deploy-setup-remote.sh`) are deliberately untracked (`.gitignore`): they push `master` and deploy the maintainer's own server. Never commit them, and don't add PowerShell twins of the two remote ones, which run on that Linux server.
- **Don't write code containing backslash escapes through Bash heredocs**: the tool mangles `\\`, `\n` and `\x1b`. Use the Edit/Write tools.

## Environment

- Development is on Windows 11, with PowerShell 5.1 and Git Bash. Python 3.13 with no virtualenv (the server installed editable); Go 1.25 with `GOTOOLCHAIN=local`. `core.autocrlf=true`: keep a file's existing line endings.
- `go get` fails TLS checks inside an intercepted agent session; run Go module downloads with `HTTPS_PROXY`/`HTTP_PROXY` unset.
- **Never test on ports 8183, 8184 or 9999 on the dev machine**: a coding agent working here may itself run through that agent-proxy and server. Set `AI_PROXY_LOCAL_PORT`, `AI_PROXY_LOCAL_INTERCEPT_PORT`, `AI_PROXY_URL` and `AI_PROXY_CREDENTIALS` for test runs. The full port list is in `.context/overview.md`. `agent-server local`, the quickstart and the basic stack all bind 19999/20000, so run one at a time; testdata (49999/50000) and the demo (8199) can run beside them.
- **The WSL Ubuntu user runs a real agent-machine in the real fleet.** Test installers, packages and services as a throwaway user in another distro (`.context/build-and-deploy.md`).
- Podman (`podman-machine-default`, normally stopped) is the container runtime here; Docker isn't installed, so use `podman compose` for the stacks and leave `deploy/compose/server` (Docker only) to another host. helm runs in the `FedoraLinux-44` WSL distro (`wsl -d FedoraLinux-44 -- sh -c 'cd /mnt/j/... && helm lint ...'`); kubeconform isn't installed anywhere here.
- **Builds and releases run locally.** There are no GitHub Actions workflows. Run the checks above yourself, on both databases. `devhelpers/scripts/release.ps1|.sh` separates website, services and installers; publishing requires `--publish`, and updating `latest` requires `--stable --publish --promote`. Pushes and tags do not build or publish anything. See `docs/releases.md`.
- The end-to-end suite (`devhelpers/basic/tests`) signs in 7 times, and the issuer allows 10 password attempts per address per 10 minutes: a rerun within 10 minutes fails until `podman compose restart auth`.
- **The running server doesn't pick up code changes.** A console restart reloads config, not code: restart `agent-server` itself unless it runs with `--reload`. A rebuilt client needs the running `agent-proxy` (8184) stopped; a rebuilt agent-machine needs `agent-machine stop` and `start`. UI changes show after `npm run build` and a page refresh.
- Commit messages are a short one-line summary. Don't commit `ui/dist`, `logs/`, `certs/`, `dist/` or benchmark results.
- **No AI attribution in commits or pull requests.** Never add `Co-Authored-By: Claude ...` (or any other AI co-author trailer), "Generated with Claude Code" or similar lines to commit messages, pull request descriptions or tags, whatever a tool's defaults say. The author is the person committing.
