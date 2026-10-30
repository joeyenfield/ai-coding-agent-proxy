# Configuration

The server keeps everything in one database: its settings, the agents it knows, accounts, the fleet, and every session's requests, payloads and terminals. That's PostgreSQL in production and a SQLite file otherwise. It also reads a set of `AI_PROXY_*` environment variables. This page covers the database, the settings and how to change them, the environment variables, the `agent-server` command line and the `agent-benchmark` runner.

Settings for agent machines (`agent`, `agent-proxy`) and fleet machines (`agent-machine`) are on [Getting started](getting-started.md) and [Machines](machines.md).

## The database

| | What it is |
|---|---|
| **PostgreSQL** | Set `AI_PROXY_DATABASE_URL`, such as `postgresql+psycopg://mesh_api:<password>@db.example:5432/mesh`. The server's tables are in schema `mesh`; agent-auth's are in `mesh_auth`, under its own role. `compose.yaml` runs one for you. See [Deployment](deployment.md#postgresql) for the roles, backups and upgrading. |
| **SQLite** | Without `AI_PROXY_DATABASE_URL`: the file `agent-server.sqlite3` in `logs/` (`AI_PROXY_LOG_DIR`). Fine for one computer, development and the tests. agent-auth keeps its own file, `issuer-state.sqlite3`, in its state folder. |

The server brings the tables up to date when it starts (`agent-server db migrate` does it alone). A new database starts with the shipped settings: listen on `0.0.0.0:9999`, HTTPS capture on, a hosted Anthropic backend as the default, the shipped agents and the shipped Ollama tuning.

| What | Where it's kept |
|---|---|
| Network settings, providers, Ollama tuning, retention | Tables `settings` and `backends` |
| Agents | Table `agents` |
| Users, organizations, teams | Tables `users` and `orgs` |
| Generated secrets: the HTTPS capture CA, the browser sign-in key, Strata containers' keys | Table `secrets` |
| Machines and their launch history | Tables `machines` and `machines_removed` |
| Sessions, request records, payloads (gzipped), chat indexes, terminal output | Tables `sessions`, `requests`, `traces`, `chat_index`, `terminals`, `terminal_chunks` |
| agent-auth's clients, codes, tokens (hashed) and signing key | Schema `mesh_auth`: `records`, `secrets` |

Three things stay files:

| File | Why |
|---|---|
| `logs/proxy/proxy.log` | The server's own log, which must work while the database is down. |
| `certs/first-user-password` | The first admin's password, for you to read once and delete. |
| `logs/ca/ca.pem`, `logs/ca/bundle.pem` | The capture CA's certificate, and it with the system roots, for agents that read them as files. Its key stays in the database. `AI_PROXY_CA_DIR` moves them. |

`agent-server db info` says which database the server uses, its size and how many sessions it holds.

### How long recordings are kept

By default, everything is kept until you delete sessions. To keep payloads and terminal output for a number of days, set it on **Server settings** → **Storage**, or with `PUT /api/config/storage`. Once an hour the server deletes the payloads, chat indexes and terminal output of sessions whose last request is older than that. Their request records and token counts stay, so totals and **Request history** still show them.

### Upgrading from files

Versions before the database kept their settings in `config/backends.yaml`, `config/models.yaml` and `config/agents.yaml`, accounts in `certs/users.json`, and recordings under `logs/`. The first start against an empty database imports all of it:

- the three YAML files (or the ones `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS` and `AI_PROXY_AGENTS` named);
- `certs/users.json`, `certs/oidc-browser-key`, `certs/container-keys.json` and the capture CA;
- `logs/machines.json`;
- every `logs/sessions/<id>/`, a transaction each. An interrupted import carries on at the next start.

agent-auth imports its `issuer.sqlite3` and `signing-key.pem` the same way, so every sign-in (`agent login`, `agent-machine login`, the console) keeps working. Nothing is deleted: once the server runs from the database, you can remove the old files. `agent-server db import --from <old install folder>` imports by hand. An agent that came with the old server and was never changed follows the new server's version from then on.

## The settings

| Setting | What it holds | Where you change it |
|---|---|---|
| `proxy` | The server's address, its public address and HTTPS | **Server settings** → **Server**, `PUT /api/config` |
| `default_backend`, `backends` | The LLM providers | **Server settings** → **Providers**, adopting a model server, the containers API |
| `intercept` | HTTPS capture | `agent-server config import`, `POST /api/config/import` |
| `models` | Tuning applied to requests sent to Ollama | **Server settings** → **Ollama model tuning**, `PUT /api/config/models` |
| `agents` | Every coding agent the server can launch | **Server settings** → **Agents**, `PUT /api/config/agents/<id>` |
| `retention_days` | How long payloads and terminals are kept | **Server settings** → **Storage**, `PUT /api/config/storage` |

Changes apply at once, except a new listen address, which waits for a restart. Only admins change them.

### All settings as one document

`agent-server config export` writes every setting as one YAML document; `agent-server config import` stores one. Use them for backups, to copy settings to another server, or to edit by hand:

```bash
agent-server config export > settings.yaml     # or: agent-server config export settings.yaml
agent-server config import settings.yaml       # only the parts the document has
agent-server config import --replace settings.yaml   # every part: what it leaves out is emptied
```

The console's API does the same (`GET /api/config/export`, `POST /api/config/import`). Everything is checked before anything is stored. A running server takes the import at once; a new listen address waits for its restart.

```yaml
proxy:
  listen_host: 0.0.0.0              # 127.0.0.1 accepts this computer only
  listen_port: 9999
  url: http://127.0.0.1:9999        # this computer's own address for the server
  public_url: https://proxy.example.net   # optional: where other devices reach it
  tls:                              # optional: serve HTTPS directly
    cert: /etc/agent-server/server.pem
    key: /etc/agent-server/server-key.pem
default_backend: laptop
backends:
  laptop:    {type: ollama, url: http://127.0.0.1:11434, machine: 3f2a9c1e7b6d4a10}
  strata-iq3:
    type: strata
    url: http://192.168.1.133:18500
    machine: 9b1c0d2e3f4a5b6c
    container: {engine: strata, image: ..., port: 18500, gpus: all, env: {...}, volume: ...}
  anthropic: {type: anthropic, url: https://api.anthropic.com}
intercept:
  enabled: true
  passthrough: []                   # host patterns tunnelled without decryption
  # listen_host: 127.0.0.1
  # listen_port: 8183               # only to accept agents directly, without an agent-proxy
retention_days: null                # keep payloads and terminals until deleted
models:
  defaults: {keep_alive: 30m, translate_openai: true, options: {num_ctx: 65536}}
  profiles: [...]
  aliases: {...}
agents:
  qwen: {name: Qwen Code, executable: qwen, protocol: openai-chat, ...}
```

### proxy

| Key | Default | What it does |
|---|---|---|
| `listen_host` | `0.0.0.0` | The address the server listens on. Anything other than loopback lets other machines in. Sign-in is required either way (see [Accounts](accounts.md)). |
| `listen_port` | `9999` | The port. |
| `url` | `http://127.0.0.1:9999` | How this computer reaches the server. The `agent-proxy` uses it before `agent login` names another, and `agent-benchmark` uses it. |
| `public_url` | none | Where other devices reach the console, such as a DNS name your router forwards here. The first-start message and the console use it ahead of this computer's own addresses. It's **Public console address** on **Server settings**. |
| `tls.cert`, `tls.key` | none | A certificate and key, as full paths. With both set, the server itself serves HTTPS. Most setups put nginx in front instead ([Deployment](deployment.md)). `AI_PROXY_TLS_CERT` and `AI_PROXY_TLS_KEY` set them too. |

### default_backend

The backend an agent gets when the launch doesn't name one, if the agent can use it. Otherwise the server picks a backend of the agent's own type first, then any other that serves it (`Settings.backend_for` in `server/agent_server/config.py`).

### backends

Each entry is a name (letters, numbers, `_` and `-`, starting with a letter or number) mapped to:

| Key | What it is |
|---|---|
| `type` | `ollama` (the default), `strata`, `docker` (Docker Model Runner) or `anthropic` (hosted). See [Providers](providers.md). |
| `url` | Where the model server answers. For a machine's backend this follows the machine's address: when the machine reports a new one, the server changes it. |
| `machine` | The id of the fleet machine that runs it. Local types (`ollama`, `strata`, `docker`) come from machines; only hosted APIs have none. |
| `container` | The container spec, when the machine runs this backend as a container (engine, image, port, gpus, env, volume). Written by the containers API; see [Machines](machines.md). |

Local backends are never added by URL. You add them from their machine: **Add as a provider** on the machine's page (`POST /api/machines/{id}/providers/adopt`, admins), or a container (`POST /api/containers`). Backends from before this have no `machine`; they keep working. An admin links a matching backend with **Add as a provider**, preserving its name and settings.

On **Server settings** you can remove local backends and pick the default among them, and add or edit hosted ones. You can't add a local backend or point one somewhere else: the server refuses it. Each backend's `machine` and `container` are kept as they are. A change to the listen address applies after a restart (**Restart the proxy**, or `POST /api/server/restart`).

### intercept

HTTPS capture for agents signed in to their own accounts (Claude, Copilot, ChatGPT). Their traffic arrives tunnelled from each machine's `agent-proxy` over the server's own port, so the server needs no extra port.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Whether the server decrypts and records account agents' HTTPS. |
| `passthrough` | `[]` | Host patterns (shell style, case-insensitive, such as `*.internal.example`) tunnelled without decryption. They're recorded as a connection only. |
| `listen_port` | none | Also accept agents directly on this port, without an `agent-proxy`. Tests and the demo use it. The listener is an open forward proxy for whoever can reach it. |
| `listen_host` | `127.0.0.1` | The address for `listen_port`. |

The server connects only to public IP addresses for intercepted traffic. It refuses loopback, private, link-local and reserved addresses, even for hosts listed under `passthrough`.

### models: Ollama tuning

Tuning for Ollama backends only. The server applies it when it translates OpenAI and Anthropic requests to Ollama's native `/api/chat`, and to native `/api/chat` and `/api/generate` calls. Strata and Docker Model Runner get requests unchanged. Edit it as one YAML document on **Server settings** → **Ollama model tuning**.

Settings are layered, in this order:

1. `defaults`.
2. The first entry in `profiles` whose `match` (a shell pattern, or a list of them) matches the Ollama model name.
3. The exact alias under `aliases`, where the key is the name agents use and `ollama_model` is the Ollama tag. (Before the database, this part of `models.yaml` was called `models`.)

Values a client sends in its request always win.

| Key | What it does |
|---|---|
| `options` | Ollama runtime options: `num_ctx`, `temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`, ... Options merge across layers. |
| `keep_alive` | How long Ollama keeps the model loaded between requests. |
| `think` | `true` or `false` to force thinking on or off for hybrid models. |
| `translate_openai` | Send OpenAI Chat Completions through native `/api/chat`, so these settings apply. Ollama's own `/v1` ignores `num_ctx`, which silently cuts long agent conversations to the server's default. |

```yaml
defaults:
  keep_alive: 30m
  translate_openai: true
  options:
    num_ctx: 65536

profiles:
  - name: qwen-coder
    match: ["qwen3*coder*", "qwen3*coding*"]
    options: {temperature: 0.7, top_p: 0.8, top_k: 20, repeat_penalty: 1.05}

aliases:
  qwen-fast:
    ollama_model: qwen3.6:35b
    think: false
```

`GET /api/models` shows the profile the server applies to each Ollama model, and lists the aliases with theirs. An Ollama backend's `num_ctx` is also the context size the server tells agents at launch (`{context_window}` in an agent's definition).

Ollama's own `OLLAMA_*` settings belong to the machine that runs it, not to the server: see [Machines](machines.md).

### agents

Every agent the launcher and the console can start: its executable, the protocol it speaks, its environment and argument templates, the files it needs, how to install it, and whether it uses its own account (`intercept: true`) or a backend (`backend_type`). The server renders these templates for every launch, so a new or changed agent is available on every agent machine at once.

The server ships with a set of agents (`server/agent_server/defaults.py`). A shipped agent follows the server's updates until an admin changes it; after that it stays as they left it, and **Reset** (or `POST /api/config/agents/<id>/reset`) takes it back to the shipped one. A shipped agent an admin removes stays removed through updates. Agents added on a server are its own.

See [Agents](agents.md) for the format and how to add one, and [MCP](mcp.md) for the `mcp` block.

## Environment variables on the server

| Variable | What it overrides |
|---|---|
| `AI_PROXY_HOME` | The install folder (see below). |
| `AI_PROXY_DATABASE_URL` | The database (see [The database](#the-database)). A secret: keep it in the environment or a secret manager. Its password is hidden wherever the server shows it. |
| `AI_PROXY_AUTH_DATABASE_URL` | agent-auth's database: its own role on the same PostgreSQL (schema `mesh_auth`). Without it, agent-auth uses the server's PostgreSQL URL, or a SQLite file of its own. |
| `AI_PROXY_HOST`, `AI_PROXY_PORT` | `proxy.listen_host` and `proxy.listen_port`. When either is set and `AI_PROXY_URL` isn't, `proxy.url` becomes `http://<host>:<port>`, with `127.0.0.1` for `0.0.0.0` and `::`. |
| `AI_PROXY_URL` | `proxy.url`. |
| `AI_PROXY_PUBLIC_URL` | `proxy.public_url`. |
| `AI_PROXY_TLS_CERT`, `AI_PROXY_TLS_KEY` | `proxy.tls.cert` and `proxy.tls.key`. |
| `AI_PROXY_DEFAULT_BACKEND` | `default_backend`. |
| `AI_PROXY_LOG_DIR` | Where the server's log goes, and the SQLite database without `AI_PROXY_DATABASE_URL` (default `logs/`). |
| `AI_PROXY_UI_DIR` | The built console (default `ui/dist`). |
| `AI_PROXY_UI_BUILD=0` | Don't rebuild the console on start when its sources are newer (the same as `--no-build`). |
| `AI_PROXY_SERVE_UI=0` | API-only server: don't serve or rebuild the console. Host the console separately; see [Deployment](deployment.md#separate-console-and-api-domains). |
| `AI_PROXY_CONSOLE_ORIGINS` | Comma-separated trusted console origins for credentialed CORS, HTTP mutations and WebSockets (default: none). Exact HTTP(S) origins only, no trailing slash, path or wildcard. |
| `AI_PROXY_DIST_DIR` | The builds the server hands out (default `dist/`). |
| `AI_PROXY_DOCS_DIR` | The documentation the console shows under **Docs** (`/help`), served at `/help-files/` (default `docs/`). |
| `AI_PROXY_VERSION` | The version the server says it is, instead of the one worked out from `VERSION` and git. |
| `AI_PROXY_INTERCEPT=0` | Turns HTTPS capture off. |
| `AI_PROXY_INTERCEPT_HOST`, `AI_PROXY_INTERCEPT_PORT` | `intercept.listen_host` and `intercept.listen_port`. |
| `AI_PROXY_CA_DIR` | Where the capture CA's certificate and bundle are written for agents (default `logs/ca/`). |
| `AI_PROXY_LIVE=0` | Stops streaming request text to the console's live views. Counts still stream. |
| `AI_PROXY_REMOTE_KEY_FILE` | Names the folder for `first-user-password` (default `certs/remote-access-key`, so `certs/`), and where an upgrade looks for the old `users.json` and keys. |
| `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS`, `AI_PROXY_AGENTS` | Read only when upgrading from files: where the old YAML files are. |
| `AI_PROXY_TOKEN` | The OAuth access token `agent-benchmark` sends; it refuses to start without one. (On agent machines the launcher reads it too, see [Agents](agents.md).) |
| `AI_PROXY_USERNAME`, `AI_PROXY_PASSWORD` | An admin account from the environment, outside the database, which the issuer accepts. With both set, the first start makes no `admin` user. |
| `AI_PROXY_NEW_PASSWORD` | The password for `agent-server user add` and `user passwd`, instead of asking. For scripts. |
| `STRATA_API_KEY` | The key sent with agents' traffic to Strata backends, ahead of the one their machine reports. A container's own key still wins. |

## How the server finds its install folder

The install folder holds `VERSION`, `logs/`, `certs/`, `ui/dist`, `dist/`, `docs/` and `benchmarks/`. The server uses `AI_PROXY_HOME` when it's set. Otherwise it takes the first of these that has `VERSION` and `server/`: the checkout the package was installed from, the current folder, then each folder above it. If none has them, it uses the current folder.

The Docker image sets `AI_PROXY_HOME=/app`.

The standalone console uses `VITE_API_URL` at build time (for example `https://api.example.net`). It routes HTTP calls, live streams, terminals, documentation and download links there. Leave it unset for same-origin bundled installs. Rebuild after changing it; this value contains no secret.

## Secrets

Every secret the server makes is in its database: the capture CA's key, the browser sign-in key and the Strata containers' API keys. Strata keys a machine reports are kept in memory only. agent-auth's signing key and hashed tokens are in its own schema, which the server's role can't read. Protect the database and its backups as you would those keys. `AI_PROXY_DATABASE_URL` and `AI_PROXY_OIDC_INTROSPECTION_SECRET` come from the environment, never from the stored settings.

To rotate the capture CA, delete its row (`intercept-ca` in table `secrets`) and restart; agents fetch the new one at their next launch.

## The agent-server command

`agent-server` is installed by `pip install -e server` (the `[project.scripts]` in `server/pyproject.toml`). It serves the API and the console on the configured address.

```bash
agent-server                       # serve, rebuilding ui/dist first if its sources are newer
agent-server --host 0.0.0.0 --port 9999
agent-server --reload --no-build   # development: restart on code changes, serve ui/dist as is
```

| Flag | What it does |
|---|---|
| `--host HOST` | Listen on this address instead of `proxy.listen_host`. Pins it: a restart from the console keeps it. |
| `--port PORT` | Listen on this port instead of `proxy.listen_port`. Pins it too. |
| `--reload` | Restart when the Python code changes. The console's restart is off in this mode. |
| `--no-build` | Serve `ui/dist` as it is, without rebuilding it. |

Without `--reload`, the server runs under a supervisor. **Restart the proxy** in the console (`POST /api/server/restart`) waits for requests in flight (up to 30 seconds), ends live streams, terminal sockets and tunnels, writes what's still queued for the database, then serves again in the same process with the stored settings read afresh, the console rebuilt if stale, and the configured address bound again. Python code changes still need `agent-server` itself restarted.

On the first start, when accounts were never set up in the database and there's no environment account, the server creates an `admin` user with a random password, prints it, and saves it to `first-user-password`. See [Accounts](accounts.md).

### Settings and the database

| Command | What it does |
|---|---|
| `config export [FILE]` | Writes every setting as YAML, to the file or to standard output. |
| `config import FILE [--replace]` | Stores a YAML (or JSON) settings document, `-` for standard input. Only the parts it has, unless `--replace`. |
| `db info` | Which database, its version, size and number of sessions, and how long recordings are kept. |
| `db migrate` | Brings the tables up to this version. The server does it on start too. |
| `db import [--from FOLDER]` | Imports an older install's files (see [Upgrading from files](#upgrading-from-files)); `--from` names its folder, holding `config/`, `certs/` and `logs/`. |

They use the same database as the server (`AI_PROXY_DATABASE_URL`, or the SQLite file in `logs/`), so run them with the server's environment.

### Users, organizations and teams

`agent-server user|org|team ...` change the accounts in the database from the server's shell. A running server, and agent-auth, pick the change up straight away.

```bash
agent-server org add pockets --name "Empty Pockets"
agent-server user add joey --org pockets --team core    # the first user is an admin
agent-server user add sam --team pockets/core           # joining a team joins its organization
agent-server user passwd sam
agent-server user list
```

| Command | What it does |
|---|---|
| `user add NAME [--admin] [--org ORG]... [--team TEAM]...` | Adds a user and asks for a password. The first user is always an admin. `--org` and `--team` repeat; a team is `<org>/<team>`, or just `<team>` with one `--org`. Organizations and teams that don't exist yet are created. |
| `user passwd NAME` | Sets a user's password and signs them out everywhere. |
| `user remove NAME` | Removes a user. Their tokens stop working. The last admin can't be removed while other users remain. |
| `user list` | Users with their role, organizations and teams. |
| `org add ID [--name NAME]` | Adds an organization. |
| `org rename ID NAME` | Changes its display name. |
| `org remove ID` | Removes it and its teams. Its members keep their accounts. |
| `org list` | Organizations and their members. |
| `team add ORG ID [--name NAME]` | Adds a team to an organization. |
| `team remove ORG ID` | Removes a team. |
| `team list [ORG]` | Teams and their members, of one organization or all. |

Names use lowercase letters, numbers, `.`, `_` and `-`, up to 40 characters. Passwords need at least 8 characters. Set `AI_PROXY_NEW_PASSWORD` to skip the prompt.

Removing every user never disables authentication. The `admin` user is made only once, on a new database. OIDC settings and all issuer environment variables are documented in [OIDC authentication](oidc.md#settings-and-persistence).

## agent-benchmark

`agent-benchmark` runs a prompt against one or more models through the server and records the timings.

```bash
agent-benchmark reasoning --model qwen3.6:35b --backend desktop
agent-benchmark --all
```

| Argument | What it does |
|---|---|
| `TEST` | A folder name under `benchmarks/tests/`. |
| `--all` | Every folder there. |
| `--model NAME` | Run this model instead of the test's `models`. |
| `--backend NAME` | Use this backend instead of the test's `backend` (default: `default_backend`). |
| `--proxy URL` | The server, instead of `proxy.url`. |
| `--tests-dir DIR`, `--results-dir DIR` | Other folders for tests (default `benchmarks/tests`) and results (default `benchmarks/results`). |

Each test is a folder with `prompt.md` (the prompt) and `test.yaml`:

| Key | Default | What it is |
|---|---|---|
| `name` | the folder's name | The test's name in the results. |
| `models` | none | The models to run. Required unless you pass `--model`. |
| `runs` | `3` | Runs per model. |
| `temperature` | `0` | Sent with each request. |
| `max_tokens` | `1024` | Sent with each request. |
| `backend` | `default_backend` | The backend to use. |
| `timeout` | `300` | Seconds to wait for each run. |

Each run opens its own session (`POST /api/sessions`), streams one OpenAI chat completion through `/session/<id>/v1/chat/completions`, ends the session and reads its telemetry. Results go to `benchmarks/results/<timestamp>/`: a JSON and Markdown file per run in a folder per test, and `summary.json` and `summary.md` with the average, minimum and maximum of each metric (tokens, time to first token, prompt and generation times and rates).

Set `AI_PROXY_TOKEN` to an active OAuth access token for benchmarks. Legacy launcher/access keys are not accepted.
