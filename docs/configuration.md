# Configuration

The server keeps its settings in three YAML files in `config/`, reads a set of `AI_PROXY_*` environment variables, and keeps its secrets in `certs/`. This page covers all of them, the `agent-server` command line and the `agent-benchmark` runner.

Settings for agent machines (`agent`, `agent-proxy`) and fleet machines (`agent-machine`) are on [Getting started](getting-started.md) and [Machines](machines.md).

## The files

| File | What it holds | Who edits it |
|---|---|---|
| `config/backends.yaml` | The server's address, its LLM providers (backends) and HTTPS capture | You, the console's **Server settings** page, adopting a model server, and the containers API |
| `config/models.yaml` | Tuning the server applies to requests sent to Ollama | You |
| `config/agents.yaml` | Every coding agent the server knows how to launch | You |

The server reads `models.yaml` and `agents.yaml` when it needs them. `backends.yaml` is read at start and on a restart from the console; the console's own changes to it apply at once.

## config/backends.yaml

```yaml
proxy:
  listen_host: 0.0.0.0              # 127.0.0.1 accepts this computer only
  listen_port: 9999
  url: http://127.0.0.1:9999        # this computer's own address for the server
  public_url: https://proxy.example.net   # optional: where other devices reach it
  tls:                              # optional: serve HTTPS directly
    cert: ../certs/server.pem
    key: ../certs/server-key.pem
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
  # ca_dir: certs
```

### proxy

| Key | Default | What it does |
|---|---|---|
| `listen_host` | `127.0.0.1` | The address the server listens on. Anything other than loopback lets other machines in, and turns sign-in on at the first such start (see [Accounts](accounts.md)). |
| `listen_port` | `8181` | The port. The repository's own config uses `9999`. |
| `url` | `http://<listen_host>:<listen_port>` | How this computer reaches the server. The `agent-proxy` uses it before `agent login` names another, and `agent-benchmark` uses it. |
| `public_url` | none | Where other devices reach the console, such as a DNS name your router forwards here. Pairing links and the first-start message use it ahead of this computer's own addresses. |
| `tls.cert`, `tls.key` | none | A certificate and key, as paths relative to the folder `backends.yaml` is in. With both set, the server itself serves HTTPS. Most setups put nginx in front instead ([Deployment](deployment.md)). |

### default_backend

The backend an agent gets when the launch doesn't name one, if the agent can use it. Otherwise the server picks a backend of the agent's own type first, then any other that serves it (`Settings.backend_for` in `server/agent_server/config.py`). Without the key the code's default is `laptop`.

### backends

Each entry is a name (letters, numbers, `_` and `-`, starting with a letter or number) mapped to:

| Key | What it is |
|---|---|
| `type` | `ollama` (the default), `strata`, `docker` (Docker Model Runner) or `anthropic` (hosted). See [Providers](providers.md). |
| `url` | Where the model server answers. For a machine's backend this follows the machine's address: when the machine reports a new one, the server rewrites it. |
| `machine` | The id of the fleet machine that runs it. Local types (`ollama`, `strata`, `docker`) come from machines; only hosted APIs have none. |
| `container` | The container spec, when the machine runs this backend as a container (engine, image, port, gpus, env, volume). Written by the containers API; see [Machines](machines.md). |

Local backends are never added by URL. You add them from their machine: **Add as a provider** on the machine's page (`POST /api/machines/{id}/providers/adopt`, admins), or a container (`POST /api/containers`). Backends from before this have no `machine`; they keep working. With accounts, an admin links a matching backend with **Add as a provider**, preserving its name and settings. Without accounts, matching legacy backends are linked automatically.

Older keys such as `metrics_url` are ignored.

### intercept

HTTPS capture for agents signed in to their own accounts (Claude, Copilot, ChatGPT). Their traffic arrives tunnelled from each machine's `agent-proxy` over the server's own port, so the server needs no extra port.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Whether the server decrypts and records account agents' HTTPS. |
| `passthrough` | `[]` | Host patterns (shell style, case-insensitive, such as `*.internal.example`) tunnelled without decryption. They're recorded as a connection only. |
| `listen_port` | none | Also accept agents directly on this port, without an `agent-proxy`. Tests and the demo use it. The listener is an open forward proxy for whoever can reach it. |
| `listen_host` | `127.0.0.1` | The address for `listen_port`. |
| `ca_dir` | `certs/` | Where the capture CA lives. |

The server connects only to public IP addresses for intercepted traffic. It refuses loopback, private, link-local and reserved addresses, even for hosts listed under `passthrough`.

### Saving from the console

**Server settings** saves with `PUT /api/config`, which rewrites `backends.yaml` with `yaml.safe_dump`. **Its comments are lost.** It writes `proxy.listen_host`, `proxy.listen_port`, `proxy.url`, `default_backend` and `backends`, and keeps the other keys (`proxy.public_url`, `proxy.tls`, `intercept`).

On that page you can remove local backends and pick the default among them, and add or edit hosted ones. You can't add a local backend or point one somewhere else: the server refuses it. Each backend's `machine` and `container` blocks are kept as they are. A change to the listen address applies after a restart (**Restart the proxy**, or `POST /api/server/restart`).

Make comment edits by hand, and expect them to go the next time someone saves from the console.

## config/models.yaml

Tuning for Ollama backends only. The server applies it when it translates OpenAI and Anthropic requests to Ollama's native `/api/chat`, and to native `/api/chat` and `/api/generate` calls. Strata and Docker Model Runner get requests unchanged.

Settings are layered, in this order:

1. `defaults`.
2. The first entry in `profiles` whose `match` (a shell pattern, or a list of them) matches the Ollama model name.
3. The exact alias under `models`, where the key is the name agents use and `ollama_model` is the Ollama tag.

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

models:
  qwen-fast:
    ollama_model: qwen3.6:35b
    think: false
```

`GET /api/models` shows the profile the server applies to each Ollama model, and lists the aliases with theirs. An Ollama backend's `num_ctx` is also the context size the server tells agents at launch (`{context_window}` in `agents.yaml`).

Ollama's own `OLLAMA_*` settings belong to the machine that runs it, not to the server: see [Machines](machines.md).

## config/agents.yaml

Every agent the launcher and the console can start: its executable, the protocol it speaks, its environment and argument templates, the files it needs, how to install it, and whether it uses its own account (`intercept: true`) or a backend (`backend_type`). The server renders these templates for every launch, so a new entry is available on every agent machine at once. The comment block at the top of the file lists every placeholder.

See [Agents](agents.md) for the format and how to add one, and [MCP](mcp.md) for the `mcp` block.

## Environment variables on the server

| Variable | What it overrides |
|---|---|
| `AI_PROXY_HOME` | The repository root (see below). |
| `AI_PROXY_HOST`, `AI_PROXY_PORT` | `proxy.listen_host` and `proxy.listen_port`. When either is set and `AI_PROXY_URL` isn't, `proxy.url` becomes `http://<host>:<port>`, with `127.0.0.1` for `0.0.0.0` and `::`. |
| `AI_PROXY_URL` | `proxy.url`. |
| `AI_PROXY_DEFAULT_BACKEND` | `default_backend`. |
| `AI_PROXY_BACKENDS`, `AI_PROXY_MODELS`, `AI_PROXY_AGENTS` | The paths of `backends.yaml`, `models.yaml` and `agents.yaml`. |
| `AI_PROXY_LOG_DIR` | Where sessions, traces, the proxy log and `machines.json` go (default `logs/`). |
| `AI_PROXY_UI_DIR` | The built console (default `ui/dist`). |
| `AI_PROXY_UI_BUILD=0` | Don't rebuild the console on start when its sources are newer (the same as `--no-build`). |
| `AI_PROXY_SERVE_UI=0` | API-only server: don't serve or rebuild the console. Host the console separately; see [Deployment](deployment.md#separate-console-and-api-domains). |
| `AI_PROXY_CONSOLE_ORIGINS` | Comma-separated trusted console origins for credentialed CORS, HTTP mutations and WebSockets (default: none). Exact HTTP(S) origins only, no trailing slash, path or wildcard. |
| `AI_PROXY_DIST_DIR` | The builds the server hands out (default `dist/`). |
| `AI_PROXY_DOCS_DIR` | The documentation the console shows under **Docs** (`/help`), served at `/help-files/` (default `docs/`). |
| `AI_PROXY_VERSION` | The version the server says it is, instead of the one worked out from `VERSION` and git. |
| `AI_PROXY_INTERCEPT=0` | Turns HTTPS capture off. |
| `AI_PROXY_INTERCEPT_HOST`, `AI_PROXY_INTERCEPT_PORT` | `intercept.listen_host` and `intercept.listen_port`. |
| `AI_PROXY_CA_DIR` | `intercept.ca_dir`. |
| `AI_PROXY_LIVE=0` | Stops streaming request text to the console's live views. Counts still stream. |
| `AI_PROXY_REMOTE_KEY_FILE` | The terminal access key's file (default `certs/remote-access-key`). `users.json`, `container-keys.json` and `first-user-password` go in the same folder. |
| `AI_PROXY_TOKEN` | Uses this value as the terminal access key instead of the file's. `agent-benchmark` also sends it as its token. |
| `AI_PROXY_USERNAME`, `AI_PROXY_PASSWORD` | An admin account from the environment. Both set turns sign-in on, without anything in `users.json`. |
| `AI_PROXY_NEW_PASSWORD` | The password for `agent-server user add` and `user passwd`, instead of asking. For scripts. |
| `STRATA_API_KEY` | The key sent with agents' traffic to Strata backends, ahead of the one their machine reports. A container's own key still wins. |

## How the server finds its root

The root is the folder holding `config/`, `logs/`, `certs/`, `ui/dist`, `dist/` and `benchmarks/`. The server uses `AI_PROXY_HOME` when it's set. Otherwise it takes the first of these that has `config/backends.yaml`: the checkout the package was installed from, the current folder, then each folder above it. If none has one, it uses the current folder.

The Docker image sets `AI_PROXY_HOME=/app`.

The standalone console uses `VITE_API_URL` at build time (for example `https://api.example.net`). It routes HTTP calls, live streams, terminals, documentation and download links there. Leave it unset for same-origin bundled installs. Rebuild after changing it; this value contains no secret.

## Secrets and state

Secrets never go in `config/`.

| File | What it is |
|---|---|
| `certs/ca.pem`, `certs/ca-key.pem` | The HTTPS capture CA and its private key, made on first use. Only the agent processes the launcher starts trust it. |
| `certs/bundle.pem` | Common root certificates plus the CA, for tools that take one CA file. |
| `certs/remote-access-key` | The terminal access key, for pairing phones on a server without accounts. |
| `certs/users.json` | Users, organizations and teams, browser sign-ins and launcher tokens. Passwords and tokens are stored only as hashes. |
| `certs/first-user-password` | The password of the `admin` user made on the first start that listens on the network. Delete it once you've changed the password. |
| `certs/container-keys.json` | The API keys the server made for the Strata containers its machines run. |
| `logs/machines.json` | The fleet: machines seen, their owners, grants, launch history and removed machines. No keys. |

Everything in the `certs/` rows lives beside the access key, so `AI_PROXY_REMOTE_KEY_FILE` moves them together. The CA follows `intercept.ca_dir`. Strata keys a machine reports are kept in memory only.

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

Without `--reload`, the server runs under a supervisor. **Restart the proxy** in the console (`POST /api/server/restart`) waits for requests in flight (up to 30 seconds), ends live streams, terminal sockets and tunnels, then serves again in the same process with `config/` read afresh, the console rebuilt if stale, and the configured address bound again. Python code changes still need `agent-server` itself restarted.

On a start that listens beyond loopback, with no `users.json` and no environment account, the server creates an `admin` user with a random password, prints it, and saves it to `first-user-password`. See [Accounts](accounts.md).

### Users, organizations and teams

`agent-server user|org|team ...` change `users.json` from the server's shell. A running server picks the change up straight away.

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
| `user remove NAME` | Removes a user, with their sign-ins and tokens. The last admin can't be removed while other users remain. |
| `user list` | Users with their role, organizations and teams. |
| `org add ID [--name NAME]` | Adds an organization. |
| `org rename ID NAME` | Changes its display name. |
| `org remove ID` | Removes it and its teams. Its members keep their accounts. |
| `org list` | Organizations and their members. |
| `team add ORG ID [--name NAME]` | Adds a team to an organization. |
| `team remove ORG ID` | Removes a team. |
| `team list [ORG]` | Teams and their members, of one organization or all. |

Names use lowercase letters, numbers, `.`, `_` and `-`, up to 40 characters. Passwords need at least 8 characters. Set `AI_PROXY_NEW_PASSWORD` to skip the prompt.

Removing every user turns sign-in off again. The `admin` user isn't created a second time, because `users.json` exists.

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

On a server with accounts, set `AI_PROXY_TOKEN` to a launcher token. Without accounts it sends this computer's access key, if it finds one.
