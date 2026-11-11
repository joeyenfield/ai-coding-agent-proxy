# Getting started

This page takes you from nothing to a first recorded agent session. You install the server, sign in, install `agent` where you run your coding agents, and start one agent on each of the three routes.

You need whichever of these you want to try:

- a Claude, GitHub Copilot or ChatGPT subscription, for the account route;
- a model server on some computer (Ollama here; llama.cpp, Strata, Docker Model Runner, LM Studio or vLLM work the same way), for the own-provider route;
- a Claude login that works with the Anthropic API, for the hosted route.

For how the pieces fit together, see [Architecture](architecture.md).

**Just want to try it?** A quickstart runs the whole system on your computer with one command: the server, the sign-in service, a database, two fleet machines with coding agents and Ollama. [Quickstart with Docker or Podman](quickstart.md) walks you from `podman compose up` to an agent working in the console in about fifteen minutes (downloads included; the tour itself is about ten), with no HTTPS or accounts to set up; [Quickstart on Kubernetes](quickstart-kubernetes.md) does the same on a local cluster with the Helm chart. To run the server on your own computer, for you alone and without containers, use [Run it on your own computer](local.md): one command. Come back here when you want a real install.

**Authentication prerequisite:** run the separate `agent-auth` issuer and configure HTTPS, callback URLs and the shared introspection secret as described in [OIDC authentication](oidc.md). API authentication is mandatory, including on loopback.

**Automated setup and test data:** after the server creates its first administrator, run `agent-auth client add initial-setup --user admin --bootstrap --label "Initial setup"` on the issuer host. Use that client ID and the secret it prints to obtain an access token and call the API. Site administrators see a warning until you revoke the bootstrap credential. [Automation](oidc.md#automation) explains the exchange and how to create, regenerate and revoke more credentials in the organisation screen.

## 1. Install the server

The server is `agent-server`, a Python program. It holds the API, the web console, accounts and every recording. Pick one way to run it.

### From a checkout

You need Python 3.11 or later (3.13 recommended), and Node.js with npm to build the console.

```bash
git clone <this repository> ai-control-mesh
cd ai-control-mesh
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ./server           # or "./server[dev]" to run the tests too
```

That installs two programs, and you run both: `agent-auth`, the sign-in service, and `agent-server`. Give both the same sign-in settings first, with your own HTTPS addresses ([OIDC authentication](oidc.md) says what each one is):

```bash
export AI_PROXY_OIDC_ISSUER=https://auth.example.com
export AI_PROXY_OIDC_AUDIENCE=https://api.example.com
export AI_PROXY_OIDC_REDIRECT_URIS=https://api.example.com/api/auth/callback
export AI_PROXY_OIDC_BROWSER_ORIGINS=https://api.example.com
export AI_PROXY_OIDC_INTROSPECTION_SECRET=<a long random secret, the same for both>

agent-server                      # the API and console, on port 9999; it makes the first account
agent-auth                        # in a second terminal: the sign-in service, on 127.0.0.1:10000
```

Start `agent-server` first: it creates the database and the first account, which `agent-auth` reads. Put your HTTPS reverse proxy in front of both, `api.example.com` to port 9999 and `auth.example.com` to port 10000.

`agent-server` keeps everything in a database: its settings, accounts, machines and every session's recordings. Without `AI_PROXY_DATABASE_URL` that's a SQLite file in the checkout, `logs/agent-server.sqlite3`; production servers use PostgreSQL (see [Deployment](deployment.md)). A new server listens on `0.0.0.0:9999`. Change its settings on the console's **Server settings** page, or with `agent-server config export` and `import` (see [Configuration](configuration.md)). On its first start, and whenever the console's sources are newer than `ui/dist`, it builds the console with npm. `agent-server --no-build` (or `AI_PROXY_UI_BUILD=0`) serves the existing build as it is.

Other options:

| Option | What it does |
| --- | --- |
| `--host`, `--port` | Listen somewhere else than the stored `proxy.listen_host` and `proxy.listen_port` |
| `--no-build` | Don't rebuild the console |
| `--reload` | Restart on Python code changes, for development (the console's restart button is off then) |

### In Docker

`deploy/compose/server/compose.yaml` pulls the project's published images, with the console inside the server's; `devhelpers/server/compose.build.yaml` builds them from the checkout instead (see [Deployment](deployment.md)). Run Compose from `deploy/compose/server`. Before the first start, put these in a `.env` file beside it: `AI_PROXY_POSTGRES_PASSWORD`, `AI_PROXY_DB_API_PASSWORD` and `AI_PROXY_DB_AUTH_PASSWORD` (letters and digits), and `AI_PROXY_OIDC_INTROSPECTION_SECRET`. Compose refuses to start without them.

```bash
docker compose up -d                  # console on http://<this host>:9999
docker compose logs agent-server      # shows the first admin's password on the first start
```

Compose runs PostgreSQL beside the API and the issuer (`agent-auth`, on `127.0.0.1:10000`), and both keep their state there: settings, accounts, machines, recordings and sign-ins. The `agent-server-logs` volume keeps the server's own log and `agent-server-certs` the first admin's password file. Keep the database and its backups private. Inside the container, `127.0.0.1` is the container itself: local providers come from machines in the fleet, not addresses the server reaches by itself. See [Deployment](deployment.md) for more.

## 2. Sign in to the console

The console always requires OIDC sign-in. **Sign in** redirects to your sign-in service (the `agent-auth` issuer you set up above); enter your password only there.

On first start, including loopback, the API creates `admin` if accounts were never set up in its database and there is no environment account. Its random password is printed and saved in the file `first-user-password` (in `certs/` normally).

1. Open your configured HTTPS console and sign in as `admin` through the issuer.
2. Reset the password with `agent-server user passwd admin`.
3. Delete `first-user-password`.

The admin is created only once, on the first start against a new database. Removing all users never disables authentication.

**Adding users from the shell.** Run these on the server's computer. Each `add` and `passwd` asks for the password. A new user is a member unless you pass `--admin`; only a user added to a new database, before the server's first start, becomes an admin by being first.

```bash
agent-server org add pockets --name "Empty Pockets"
agent-server user add joey --org pockets --team core --admin
agent-server user add sam --team pockets/core             # joining a team joins its organization
agent-server user list
```

| Command | What it does |
| --- | --- |
| `agent-server user add <name> [--admin] [--org <org>]... [--team <org>/<team>]...` | Add a user. Organizations and teams are created if new. `--team <team>` works without the organization when you give exactly one `--org`. |
| `agent-server user passwd <name>` | Set a user's password, which signs them out everywhere |
| `agent-server user remove <name>` | Remove a user |
| `agent-server user list` | List users with their role, organizations and teams |
| `agent-server org add <id> [--name <display name>]` | Add an organization |
| `agent-server org rename <id> <name>` | Rename one |
| `agent-server org remove <id>` | Remove one; its members keep their accounts |
| `agent-server org list` | List organizations and their members |
| `agent-server team add <org> <id> [--name <display name>]` | Add a team to an organization |
| `agent-server team remove <org> <id>` | Remove a team |
| `agent-server team list [<org>]` | List teams and their members |

The console's **Organizations** page does the same.

## 3. Install `agent` where you run agents

`agent` is the client, a single Go binary. It starts coding agents and runs that computer's `agent-proxy`, which carries their traffic to the server. Install it on every computer where you'll start an agent, the server's own included.

**With the one-line installer.** A running server serves an installer for its address and build. It verifies downloaded archives, installs the programs and starts OIDC device sign-in. Approve the printed codes in your browser; the installer never collects your password.

```bash
curl -fsSL http://server:9999/install.sh | sh                        # Linux, macOS, FreeBSD
curl -fsSL http://server:9999/install.sh | sh -s -- --only agent      # just agent
```

```powershell
irm http://server:9999/install.ps1 | iex                             # Windows
& ([scriptblock]::Create((irm http://server:9999/install.ps1))) -Only agent
```

| Option (sh / PowerShell) | What it does |
| --- | --- |
| `--only agent` or `--only agent-machine` / `-Only` | Install just one of them (default: both) |
| `--service` / `-Service` | Start `agent-machine` at boot or sign-in |
| `--prefix DIR` / `-Prefix` | Install somewhere else (default: `/usr/local/bin` as root, else `~/.local/bin`; on Windows `%LOCALAPPDATA%\Programs\agent-proxy`) |
| `--no-login` / `-NoLogin` | Don't sign in; print the commands instead |
| `--no-start` / `-NoStart` | Don't start `agent-machine` after signing it in |
| `--python` / `-Python` | Install the Python versions instead, on any system with Python 3.9 or later |
| `--no-tray` / `-NoTray` | Leave out the tray icon, which is otherwise put in on a desktop with Python 3.9 or later |

The installer only works when the server has builds for its version in `dist/<version>/`. A new server has none. Fetch its release's, each checked against the release's SHA-256:

```bash
agent-server downloads fetch                                              # on the server's computer
docker compose exec --user 0 agent-server agent-server downloads fetch    # in Compose, from deploy/compose/server
```

It takes the release of the server's own `X.Y.Z` from GitHub (`--version` names another, `--from <url>` another place), and a running server offers the builds at once. Or build them from a checkout with `./devhelpers/scripts/build-dist.sh` (or `.\devhelpers\scripts\build-dist.ps1`), which needs Go. In Docker, Compose mounts the checkout's `dist/` into the container.

**From the Downloads page.** The console's **Downloads** page (`/get`) lists the same builds per platform, with their sizes, SHA-256 and the commands to install them. It's public, so a new computer can use it before signing in. Each archive has its own `install.sh` or `install.ps1` that copies the programs onto your `PATH`.

**Building it yourself.** With Go 1.24 or later, `./devhelpers/scripts/build-client.sh` (or `.\devhelpers\scripts\build-client.ps1`) builds `client/bin/agent`. Put it on your `PATH`.

A computer without a Go build gets the Python port instead: the one-line installer switches to it by itself, or use `--python` (`-Python`) to choose it. It installs the same commands (`agent`, `agent-proxy`, `agent-machine`) from the wheel the server offers, checked against its SHA-256, into an environment of its own. See [Deployment](deployment.md#the-one-line-installers).

## 4. Sign the computer in

On every computer where you run agents, the server's own included, sign in once:

```bash
agent login https://your-server.example.com
```

Open the printed OIDC verification URL in your browser and approve the device code, or, if you're signed in to the console, type the code on **Machines › Approve a device**. Access refreshes automatically; legacy access-key and password options are removed.

`agent login` and `agent logout` take effect at once, with no restart.

Check it:

```bash
agent --list          # the server's agents, whether each is installed here, and the server and user in use
```

The first `agent` command starts this computer's `agent-proxy` in the background if it isn't running. When the server is on this computer, isn't running and `agent-server` is on your `PATH`, it starts that too.

## 5. Start a first agent

Run `agent <agent> <folder>` from your project. The agents and how to start them come from the server's stored agent definitions, so every signed-in computer has the same list. Install the coding agent itself first (`agent --list` shows the install command). Arguments after `--` go to the agent.

Each route below records the session on the server. Add `--trace` to save full request and response bodies, and `--control` to share the agent's terminal with the console.

### Your own account

Claude Code, Copilot CLI or Codex, signed in to your own subscription. The agent works as usual, and the server records every HTTPS request it makes, not only model calls.

Sign in once with the agent itself (`claude`, `copilot` then `/login`, or `codex login`). Unset `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `COPILOT_PROVIDER_*`, `COPILOT_OFFLINE`, `OPENAI_API_KEY` and `OPENAI_BASE_URL`, and remove any Claude `apiKeyHelper`, so the account login is the one used. Then:

```bash
agent claude-account .
agent copilot-account .
agent codex-account .
agent claude-account . --trace -- --model sonnet    # save bodies; pass a model to Claude Code
```

The launcher gives only the agent's own processes the server's CA, so nothing is added to your system's trust store.

### Your own provider, on Ollama

A provider of your own is a model server on one of your **machines** (or a hosted API you add with its key; see [Providers](providers.md)). This walk uses Ollama, but any model server a machine runs is added the same way. They come from **machines**: computers in the server's fleet, running `agent-machine`. On the computer that runs Ollama (it can be the server's, or the one you work on):

```bash
agent-machine login https://your-server.example.com   # approve its separate device code
agent-machine start                         # join the fleet in the background
agent-machine                               # how the fleet sees this machine
```

The machine shows up under **Machines** in the console, with the Ollama it found. An admin opens it and picks **Add as a provider**. The new backend takes the machine's name. Then, from any signed-in computer:

```bash
agent qwen . --model qwen3:8b                         # Qwen Code on the default Ollama backend
agent opencode . --model qwen3:8b --backend <name>    # pick a backend by name
agent claude . --model qwen3:8b                       # Claude Code, translated to Ollama
```

Local-model agents need `--model`. The server translates their requests to Ollama's native API and applies the context size and sampling from the server's Ollama model tuning. Without `--backend`, the server uses `default_backend` when the agent can use it, otherwise the first backend that fits. See [Providers](providers.md) for llama.cpp, Strata, Docker Model Runner and containers, and [Machines](machines.md) for the fleet.

### The hosted Anthropic API

`claude-subscription` points Claude Code's `ANTHROPIC_BASE_URL` at the server, which forwards each request unchanged to the `anthropic` backend (`https://api.anthropic.com`, in a new server's settings). Claude Code keeps its own login.

```bash
agent claude-subscription . --model sonnet
```

The server records Claude Code's API calls, but not its sign-in, telemetry or MCP traffic, which go straight to Anthropic. Use `claude-account` when you want those too.

## 6. See what was recorded

Open the console:

- **Sessions** has one row per agent run, with its agent, project, model and requests.
- **Traffic** lists every request, like a browser's network tab.
- **Live** streams model calls token by token while they run.
- **Running agents** shows the terminals of agents started with `--control`, to read and type into, from your phone too.

Everything is in the server's database: each session, a record per request and, with `--trace`, the full payloads. **Server settings** → **Storage** can delete payloads and terminals after a number of days.

## Where to go next

- [Agents](agents.md): every supported agent, launch options, sharing sessions, and adding an agent of your own.
- [Machines](machines.md): the fleet, starting agents on a machine from the console, remote terminals and updates.
- [Providers](providers.md): Ollama tuning, llama.cpp, Strata, Docker Model Runner and containers.
- [MCP](mcp.md): sharing a machine's MCP servers with agents.
- [Console](console.md): every page of the console.
- [Accounts](accounts.md): users, organizations, teams, sharing and phone sign-in.
- [Configuration](configuration.md): the database, the stored settings, `agent-server config` and environment variables.
- [Deployment](deployment.md): Docker, HTTPS, services and packages.
