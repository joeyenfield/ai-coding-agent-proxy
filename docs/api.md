# API

Everything the server does is an HTTP or WebSocket API on its one port. The console uses it, and so do the launcher (`agent`), each machine's `agent-proxy` and `agent-machine`. This page lists every route the server registers.

The server also describes its HTTP routes in OpenAPI:

| Path | What it is |
|---|---|
| `/openapi.json` | The OpenAPI document, grouped by area, with a unique id per operation and the sign-in schemes. |
| `/docs` | Swagger UI. Use **Authorize** with a launcher token, or just be signed in to the console. |
| `/redoc` | ReDoc. |

With accounts, these need a sign-in like the rest of the API.

## Who may call what

| Label | Means |
|---|---|
| **Public** | Anyone, always. |
| **Session** | An agent naming an open session: in the path (`/session/<id>/...`), or with `X-AI-Proxy-Session` on `/v1` and `/anthropic`. The session id is the credential. |
| **Signed in** | With accounts: a browser sign-in (the `ai_proxy_session` cookie) or a launcher token (`X-AI-Proxy-Token`). Without accounts: anyone who reaches the port. |
| **Admin** | Signed in as an admin: the route is in `auth.ADMIN_ONLY`. Without accounts there are no admins and the label means nothing extra. |
| **Trusted** | Without accounts: this computer (a loopback client with a loopback `Host`) or a request with the access key (the `ai_proxy_remote` cookie or `X-AI-Proxy-Token`). With accounts: signed in. |
| **Local** | Without accounts, this computer only. With accounts, refused. |
| **Same origin** | Unsafe methods and WebSockets from another site's page (a different `Origin`) are refused. |
| **Machine feature** | The fleet's grants, `fleet.allowed(identity, machine, feature)`: the machine's owner always; admins for `view`, `models` and `update`; organizations and teams the owner granted the feature. Without accounts, everyone. See [Machines](machines.md). |
| **Machine owner** | The machine's owner, or an admin where it says so. |

With accounts, every path under `/api/`, `/v1/`, `/anthropic/`, `/session/`, `/docs`, `/redoc` and `/openapi.json` needs a sign-in, except `/health`, `/api/auth/status`, `/api/auth/login`, `/api/auth/logout` and `/api/auth/token`, and agent requests that name an open session. A refused request gets `401`, or `403` for another site's unsafe request or a non-admin on an admin route. A refused WebSocket is closed with `4401` or `4403`.

Every endpoint that touches a session checks its sharing (see [Accounts](accounts.md)). A session you can't see answers `404`.

**Versions.** `agent` and `agent-machine` send `X-AI-Proxy-Version` and `X-AI-Proxy-Protocol` with every request. A protocol outside the server's `MIN_PROTOCOL..PROTOCOL` gets `426 Upgrade Required` with how to update (WebSockets close with `4426`). Requests without the header, such as browsers', aren't checked.

## Agent-facing APIs

What agents call. On an agent machine they call the same paths on their `agent-proxy` (`http://127.0.0.1:8184`), which streams them to the server without its token. All are **Session**: the scoped paths under `/session/{session_id}/` need an open session; the unscoped ones take `X-AI-Proxy-Session` and `X-AI-Proxy-Backend`. An unscoped request without a session gets a new implicit session on the default backend; with accounts that needs a sign-in, and the session is the user's, private.

| Method | Path | What it does |
|---|---|---|
| GET, POST | `/v1/{path}`, `/session/{session_id}/v1/{path}` | OpenAI: chat completions, responses, embeddings and the rest. `GET .../models` lists the backend's models. On an Ollama backend, chat completions are translated to native `/api/chat` with the model's profile applied; Strata and Docker Model Runner get them unchanged. |
| POST | `/anthropic/v1/messages`, `/session/{session_id}/anthropic/v1/messages` | Anthropic Messages: translated for Ollama, passed through to Anthropic, Strata and Docker Model Runner. |
| POST | `/anthropic/v1/messages/count_tokens`, `/session/{session_id}/anthropic/v1/messages/count_tokens` | Token counting: passed through where the backend has it, estimated for Ollama. |
| GET, POST, PUT, PATCH, DELETE | `/anthropic/{path}`, `/session/{session_id}/anthropic/{path}` | Any other Anthropic path, forwarded only with a hosted `anthropic` backend. |
| GET, POST, DELETE | `/api/{path}`, `/session/{session_id}/api/{path}` | Native Ollama, for what agents call only: `chat`, `generate`, `embed`, `embeddings`, `tags`, `show`, `ps`, `version`. Profiles apply to `chat` and `generate` on Ollama; Docker Model Runner's Ollama API gets them unchanged; Strata has none. Anything else (`pull`, `delete`, ...) answers `404`: models are managed through their machine. With accounts, the unscoped `/api/{path}` needs a sign-in, so agents use the session path. |
| GET, POST, DELETE | `/session/{session_id}/mcp/{machine_id}/{name}` | A shared MCP server, as Streamable HTTP, relayed to its machine through its tunnel. Only the servers the session was started with; whoever started it must still hold the machine's `mcp` grant. Each POST is recorded in the session with its reply. See [MCP](mcp.md). |

Account agents don't use these. Their HTTPS goes to the `agent-proxy`'s HTTPS proxy (`HTTPS_PROXY=http://<session>:agent-proxy@127.0.0.1:8183`), which carries each connection to `WS /api/proxy/tunnel` (below).

The catch-alls here are registered after every console route, so `/api/{path}` never shadows one.

## Health and the server

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/health` | Public | `status`, the running instance's id, the server's `version`, `protocol` and `min_protocol`. |
| GET | `/api/status` | Signed in | Totals, requests in flight, recent requests, intercept state, `default_backend`, whether a restart is needed for the listen address, and each machine backend's `provider`. With accounts, only sessions you can see. |
| GET | `/api/config` | Signed in | The editable network settings (`proxy.listen_host`, `listen_port`, `url`, `default_backend`, `backends`) and `restart_required`. |
| PUT | `/api/config` | Admin | Saves them to `backends.yaml`. Can remove local backends and add or edit hosted ones; refuses new or moved local ones. Without `backends`, leaves them alone. See [Configuration](configuration.md). |
| POST | `/api/server/restart` | Admin, Trusted, Same origin | Restarts with the configuration on disk once in-flight requests finish (up to 30 s). Answers `202` with `waiting_for` and the address it comes back on. `409` when the server can't restart itself (`--reload`). |

## Sessions and requests

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/api/sessions` | Signed in | Opens a session for other tools: `client`, `project`, `model`, `backend` (or `direct`), `trace`, `tags`, and with accounts `visibility`, `team`, `org`, `access`. A `shell` client is always private. |
| GET | `/api/sessions` | Signed in | The sessions you can see, each with its `machine` and what `you` may do. |
| DELETE | `/api/sessions` | Signed in | Deletes all saved traffic. Ended sessions and open ones idle for 15 minutes are deleted; recently active ones keep running but lose their data; ones with requests in flight are skipped. With accounts, only your own (and admins: sessions without an owner). |
| GET | `/api/sessions/{session_id}` | Signed in, can view | One session. |
| PATCH | `/api/sessions/{session_id}` | Signed in, can control | Ends it (`ended`, `exit_status`), switches capture (`trace`), or for its owner changes its sharing (`visibility`, `team`, `org`, `access`). Narrowing the sharing drops terminal viewers who lose access. A shell can't be shared. |
| DELETE | `/api/sessions/{session_id}` | Signed in, can control | Deletes it. `409` while requests are in flight. |
| DELETE | `/api/sessions/{session_id}/data` | Signed in, can control | Clears its saved requests and traces, keeping the session. |
| GET | `/api/requests` | Signed in | Every request you can see, newest first, with `trace_available`. `?session_id=` for one session. |
| GET | `/api/sessions/{session_id}/telemetry` | Signed in, can view | The session's `requests.jsonl` as a download. |
| GET | `/api/sessions/{session_id}/traces` | Signed in, can view | The names of its saved traces. |
| GET | `/api/sessions/{session_id}/traces/{request_id}` | Signed in, can view | One trace: the full request and response. |
| GET | `/api/sessions/{session_id}/chat` | Signed in, can view | The session's traced model calls read back as chats: its threads, and a page of one thread's turns. `?thread=`, `?before=`, `?limit=` (1 to 500, default 60). |
| GET | `/api/sessions/{session_id}/chat/turns/{position}` | Signed in, can view | One turn in full, where a page had only a preview. `?thread=` is required. |

## Live traffic

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/live` | Signed in | Server-sent events: a `snapshot`, then `start`, `delta` and `end` events for each request, and keep-alive comments every 15 s. `?session_id=` for one session. Sharing is checked per event. |
| GET | `/api/live/{session_id}/{request_id}` | Signed in, can view | Payloads, headers and the latest raw chunks of one live or recently finished request. `404` once it's left the live view. |

`AI_PROXY_LIVE=0` stops the request text streaming; counts still do.

## Agents and models

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/agents` | Signed in | Every agent in `config/agents.yaml`, with the `backends` it can use and the `default_backend` a launch picks (`direct` for account agents). |
| POST | `/api/agents/{agent_id}/sessions` | Signed in | Opens a session for an agent and returns `session`, the rendered `env`, `command` and `files`, `container` (whether its backend is a container to start first), `mcp` (the shared MCP servers it got, with their URLs) and `shell` (the same as bash and PowerShell lines). Takes `model`, `backend`, `trace`, `project`, `project_path`, `mcp`, sharing, `tags`; the launcher adds `launcher: true`, its `proxy_url`, its `intercept` paths and `files_dir`. `409` for an account agent when capture is off. |
| GET | `/api/models` | Signed in | The models on each backend (or `?backend=` for one), asked of the backend's machine, with the profile the server applies, plus `aliases` from `models.yaml`. |
| GET | `/api/mcp` | Signed in | The MCP servers machines share that you may give agents. |

## HTTPS capture

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/intercept` | Signed in | Whether capture is running, its settings, and the CA's path and fingerprint. |
| GET | `/api/intercept/ca.pem` | Signed in | The capture CA's certificate. `404` when capture isn't running. |
| GET | `/api/intercept/bundle.pem` | Signed in | Common root certificates plus the CA, for tools that take one CA file. `404` when capture isn't running. |

## Machines and their providers

Every machine route needs **Trusted**. A machine you may not see answers `404`. The server asks the machine over its fleet socket for everything about a model server; see [Machines](machines.md) and [Providers](providers.md). A model server is named `{kind}-{port}`, such as `ollama-11434`, `strata-18500` or `docker-12434`.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/machines` | Trusted | The machines you can see (connected first), the ones you removed, and the list of `features`. |
| GET | `/api/machines/{machine_id}` | Trusted, `view` | One machine: what it runs and reports, its backends, launch history, and what `you` may do. |
| DELETE | `/api/machines/{machine_id}` | Owner or admin, Same origin | Removes it from the fleet, connected or not. It's refused when it connects again until restored. |
| POST | `/api/machines/{machine_id}/restore` | Owner or admin, Same origin | Lets a removed machine join again. |
| PUT | `/api/machines/{machine_id}/access` | Owner or admin, Same origin | Sets which organizations and teams may use each feature: `{"access": {feature: {"orgs": [...], "teams": [...]}}}`. Members grant only their own organizations and teams. |
| POST | `/api/machines/{machine_id}/launch` | `launch`, Same origin | Starts an agent there in a shared terminal: `agent`, `project` (inside one of its roots), `model`, `backend`, `trace`, `visibility`, `team`, `access`, `mcp`. Needs `agent-machine launches on`. Answers `201` once the session is open; someone other than the owner is tagged `launched_by`. |
| POST | `/api/machines/{machine_id}/shell` | `terminal`, Same origin | Opens a shell there in a private terminal: `project` (a folder in its roots; default the first root). Needs `agent-machine commands on`. |
| POST | `/api/machines/{machine_id}/update` | `update`, Same origin | Has the machine update `agent-machine` (and with `"agent": true`, `agent`) to the server's build, then restart. Needs `agent-machine updates on`. |
| GET | `/api/machines/{machine_id}/folders` | `launch` or `terminal` | Folder names inside one of its launch folders, `?path=`. Without a path, its roots. |
| POST | `/api/machines/{machine_id}/providers/adopt` | Admin | Adds a model server the machine runs as a backend: `{"type": "ollama"}`. `409` if a backend already uses it. |
| GET | `/api/machines/{machine_id}/providers/{provider_id}` | `view` | The model server as the machine sees it now (models, what's loaded, status), its backend, and the traffic agents sent it. |
| POST | `/api/machines/{machine_id}/providers/{provider_id}/models/{action}` | `models`, Same origin | `pull`, `delete`, `load` or `unload` a model: `{"name": ...}`. Strata only loads and unloads. `pull` streams the machine's progress as NDJSON lines, ending with `{"status": "success"}` or `{"status": "error"}`. |
| GET | `/api/machines/{machine_id}/providers/{provider_id}/log` | `view` | The end of its log on the machine: `?source=server` or `engine` (Strata), `?tail=` bytes. |
| GET | `/api/machines/{machine_id}/metrics` | `view` | The machine's CPU, memory and GPU load, sampled there. |
| GET | `/api/machines/{machine_id}/catalog/{catalog}` | `view` | Searches a model catalogue from the machine: `ollama`, `huggingface`, `huggingface-quants`, `docker-hub` or `docker-hub-tags`, with `?q=` and `?repo=`. |
| GET | `/api/machines/{machine_id}/servers/ollama` | `view` | The settings `agent-machine` starts the machine's Ollama with, and its state. |
| PUT | `/api/machines/{machine_id}/servers/ollama` | `servers`, Same origin | Saves them; they apply when Ollama next starts. Needs `agent-machine servers on`. |
| POST | `/api/machines/{machine_id}/servers/{kind}/{action}` | `servers`, Same origin | Starts, stops or restarts the machine's Ollama, or starts or stops a Strata profile (`{"profile": ...}`). Needs `agent-machine servers on`. |

## Containers

Containers the fleet's machines run as backends. See [Machines](machines.md). Changes also need **Same origin**.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/containers` | Trusted | The machines you can see that could run containers (their CLI, whether it answers, the engines they offer), and every container's state. |
| GET | `/api/containers/{name}` | Trusted | One container's state. |
| POST | `/api/containers` | Admin, machine owner | Creates one on a connected machine with `agent-machine containers on`: `name`, `machine`, `engine`, and `image`, `port`, `gpus`, `env`, `volume`. Saves it as a backend. |
| PUT | `/api/containers/{name}` | Admin, machine owner | Changes its settings; they apply when it next starts. The engine can't change. |
| DELETE | `/api/containers/{name}` | Admin, machine owner | Removes it from its machine and from `backends.yaml`. |
| POST | `/api/containers/{name}/{action}` | Trusted | `start`, `stop` or `restart`. Answers `202` with the job. `409` with `conflicts` when other containers hold the GPU it needs; send `{"switch": true}` to stop them first. |
| GET | `/api/containers/{name}/log` | Trusted | Its container log, `?tail=` lines (10 to 5000, default 400). |

## Terminals and pairing

Shared terminals are a remote shell, so these routes need **Trusted** and **Same origin**. See [Accounts](accounts.md).

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/remote` | Anyone | Whether this browser is `trusted`, `local`, `paired`, and whether the server has `accounts`. On this computer only, also the access `key`, the bound host and port, and the `addresses` a phone could use (the `public_url` first). |
| POST | `/api/remote/code` | Local, Same origin | A one-time pairing code (`ABCD-EFGH`), valid for `expires_in` seconds (10 minutes). |
| POST | `/api/remote/pair` | Same origin | Pairs this browser: `{"key": ...}`, the access key or a pairing code. Sets the `ai_proxy_remote` cookie. `403` when it doesn't match. |
| POST | `/api/remote/unpair` | Same origin | Removes this browser's cookie. |
| POST | `/api/remote/rotate` | Admin, Local, Same origin | Replaces the access key, which signs every paired device out. |
| GET | `/api/terminals` | Trusted | The shared terminals you can see: agent, machine, project, model, backend, state, activity, requests in flight and `attention` (waiting, with the question on screen). |

The two terminal WebSockets are below.

## Accounts, organizations and users

See [Accounts](accounts.md). Changes answer with the whole directory (`orgs` and `users`) as you may see it. The organization and user routes answer `400` on a server without accounts.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/auth/status` | Public | `required` (whether sign-in is on), the signed-in `identity`, and whether the connection is HTTPS. |
| POST | `/api/auth/login` | Public | `{"username", "password"}`. Sets the `ai_proxy_session` cookie for 30 days. Ten failures in ten minutes from one client answer `429`. |
| POST | `/api/auth/logout` | Public | Ends this browser's sign-in. |
| POST | `/api/auth/token` | Public | `agent login`: a username, password and `label` in, a launcher token out (`id`, `token`, `user`). Same failure limit. |
| GET | `/api/auth/tokens` | Signed in | Your launcher tokens (not the secrets). |
| POST | `/api/auth/tokens` | Signed in | A new launcher token for you, with a `label`. The token is shown once. |
| DELETE | `/api/auth/tokens/{token_id}` | Signed in | Revokes one of yours. |
| POST | `/api/auth/password` | Signed in | `{"current", "new"}`: changes your password and signs you out everywhere. |
| GET | `/api/orgs` | Signed in | Organizations with their teams and members. Members see only their own. |
| POST | `/api/orgs` | Admin | `{"id", "name"}`: adds one. |
| PUT | `/api/orgs/{org}` | Admin | `{"name"}`: renames it. |
| DELETE | `/api/orgs/{org}` | Admin | Removes it and its teams; members keep their accounts. |
| PUT / DELETE | `/api/orgs/{org}/members/{name}` | Admin | Adds/removes one user's organization membership. Removal also leaves its teams, without changing memberships elsewhere. No body needed. |
| POST | `/api/orgs/{org}/teams` | Admin | `{"id", "name"}`: adds a team. |
| PUT | `/api/orgs/{org}/teams/{team}` | Admin | `{"name"}`: changes the display name, keeping its id and sharing key. |
| DELETE | `/api/orgs/{org}/teams/{team}` | Admin | Removes a team. |
| PUT / DELETE | `/api/orgs/{org}/teams/{team}/members/{name}` | Admin | Adds/removes one team membership. Adding also joins its organization; removal keeps organization membership. No body needed. |
| GET | `/api/users` | Signed in | Users with their roles, organizations and teams. Members see themselves and the people in their organizations. |
| POST | `/api/users` | Admin | `{"name", "password", "role", "orgs", "teams"}`: adds one. |
| PUT | `/api/users/{name}` | Admin | Changes `role`, `orgs`, `teams` or `password` together, only if all changes are valid. The last admin can't be made a member. |
| DELETE | `/api/users/{name}` | Admin | Removes a user. You can't remove yourself. |

## Downloads and installers

All **Public**, so a new machine or a phone can get them before signing in. See [Deployment](deployment.md).

| Method | Path | What it does |
|---|---|---|
| GET | `/downloads/index.json` | Every file of the build the server hands out, with its platform, component, kind, size, SHA-256 and URL, the server's version and protocol, and a hint when nothing is built. |
| GET | `/downloads/{wanted}/{path}` | One file of that build, only if its `SHA256SUMS` lists it. |
| GET | `/downloads/android.apk` | Redirects to the build's Android app. `404` without one. |
| GET | `/android` | The same redirect, a short address to type on a phone. Not in OpenAPI. |
| GET | `/install.sh` | The one-line installer for Linux, macOS and FreeBSD, written for the address you reached the server at and its build's archives: `curl -fsSL <server>/install.sh \| sh`. |
| GET | `/install.ps1` | The same for Windows: `irm <server>/install.ps1 \| iex`. |

## The console

| Method | Path | What it does |
|---|---|---|
| GET | `/` | The console's `index.html`, or a page saying how to build it. |
| — | `/assets/...` | The console's static files (a mount, present once `ui/dist` is built). |
| GET | `/help-files/{path}` | Public: one file of the documentation (`docs/`, or `AI_PROXY_DOCS_DIR`) for the console's Docs page (`/help`): a Markdown page (`text/markdown`) or a picture (`.png`, `.jpg`, `.svg`). Anything else, or outside the folder, answers `404`. |
| GET | `/{path}` | A file from `ui/dist`, else `index.html` for the console's own routes. Paths starting with `api/`, `v1/`, `session/`, `anthropic/`, `downloads/` or `help-files/` answer `404`. |

The console's pages, sign-in included, are public files; the data they show comes from the API above.

## OpenAPI

| Method | Path | What it does |
|---|---|---|
| GET | `/openapi.json` | The OpenAPI document. |
| GET | `/docs` | Swagger UI. |
| GET | `/docs/oauth2-redirect` | Swagger UI's sign-in redirect page. |
| GET | `/redoc` | ReDoc. |

## WebSockets

OpenAPI can't describe these. All need **Trusted** (with accounts, a sign-in; machines send their own token) and **Same origin**.

| Path | Who connects | What it carries |
|---|---|---|
| `/api/machines/connect` | `agent-machine run`, with its token | One machine in the fleet, for as long as it runs. In: its `hello` (id, name, installed agents, model servers, containers and engines, launch and server settings, shared MCP servers), then `update` every minute, `ping`, and answers (`launched`, `launch_failed`, `container_result`, `server_result`, `provider_result`, `provider_progress`, `folders_result`, `update_result`). Out: `welcome`, then requests: `launch`, `shell`, `tunnel`, and `container`, `server`, `provider`, `folders` and `update` requests with an `op`. A removed machine is closed with `4403`. |
| `/api/machines/tunnel/{tunnel_id}` | `agent-machine`, with its token, when the server asks | One connection of agents' traffic (or MCP traffic) to a port on that machine: raw bytes both ways. Closed with `4404` for an unknown tunnel. |
| `/api/proxy/tunnel` | `agent-proxy`, with its machine's token | One account agent's HTTPS connection, which the server decrypts and records with its CA. Binary frames both ways. Closed with `4404` when capture is off. |
| `/api/terminals/{session_id}/host` | The launcher (`--control`), relayed by its `agent-proxy` | The launcher sends a hello, then the agent's output as binary frames and `resize`, `gap` and `exit` messages. The server sends `resume` (where to resend from) and `input` (`parts` to type, sent a short gap apart). Only someone who can control the session may host it. |
| `/api/terminals/{session_id}/view` | The console's terminal page | The server sends `state`, the history as binary frames, `replayed`, then live output and `state`, `resize` and `error` messages. The viewer sends `{"type": "input", "text": "...", "enter": true}` or `{"type": "input", "keys": "\u001b"}`; typing needs control of the session, checked on every message. A viewer that falls behind is closed with `4408`, and a missing terminal with `4404`. |

## Check

This page lists all 105 routes the app registers, from `app.routes` of a server built in a temporary folder: 100 HTTP routes, the 5 WebSockets above, plus the `/assets` static mount.

| Area | Routes |
|---|---|
| Agent-facing | 10 (5 paths, each unscoped and under `/session/{session_id}/`) plus the MCP relay |
| Health and the server | 5 |
| Sessions and requests | 13 |
| Live traffic | 2 |
| Agents and models | 4 (with `/api/mcp`) |
| HTTPS capture | 3 |
| Machines and providers | 18, plus 2 WebSockets |
| Containers | 7 |
| Terminals and pairing | 6, plus 2 WebSockets |
| Accounts | 18 |
| Downloads | 6 |
| Console | 3 (with the documentation's files) |
| OpenAPI | 4 |
| Proxy tunnel | 1 WebSocket |

That is 10 + 1 + 5 + 13 + 2 + 4 + 3 + 20 + 7 + 8 + 18 + 6 + 3 + 4 + 1 = 105. Each is in a table above.
