# API

Everything the server does is an HTTP or WebSocket API on its one port, used by the console, the launcher (`agent`), each computer's `agent-proxy` and `agent-machine`. This page lists every route.

The server also describes its HTTP routes in OpenAPI:

| Path | What it is |
|---|---|
| `/openapi.json` | The OpenAPI document, grouped by area, with a unique id per operation and the sign-in schemes. |
| `/docs` | Swagger UI. Use **Authorize** with an OAuth access token, or just be signed in to the console. |
| `/redoc` | ReDoc. |

These always need an OAuth access token or an authenticated browser cookie. See [OIDC authentication](oidc.md) for the separate issuer's endpoints.

With [separate hosting](deployment.md#separate-console-and-api-domains), all these paths live on the API domain, not the static console. The console sends credentials on HTTP and live-stream requests; terminal WebSockets use the API host's cookie. `AI_PROXY_CONSOLE_ORIGINS` explicitly trusts the console origin for CORS, mutations and WebSockets.

## Who may call what

| Label | Means |
|---|---|
| **Public** | Anyone, always. |
| **Session** | Access-token authentication plus control permission for the named open session. MCP additionally requires its launch-only capability. |
| **Signed in** | Active OAuth access token: `Authorization: Bearer`, `X-AI-Proxy-Token`, or host-only `ai_proxy_session` cookie. |
| **Site admin** | Signed in as an admin of the root organization; enforced by `auth.SITE_ADMIN_ONLY`. |
| **Admin** | Signed in as an admin of an organization, and only for that organization's things (site admins: also the users and teams of any organization); enforced by `auth.ADMIN_ONLY` and the route. |
| **Trusted** | Signed in. Loopback and old access keys confer no privilege. |
| **Same origin** | Unsafe methods and WebSockets require the request's host or an exact console origin in `AI_PROXY_CONSOLE_ORIGINS`. Other origins are refused. |
| **Machine feature** | The fleet's grants, `fleet.allowed(identity, machine, feature)`, only ever inside the machine's organization: the machine's owner always; its organization's admins for `view`, `models` and `update`; the organization or teams the owner granted the feature. Site admins have every feature on every machine. See [Machines](machines.md). |
| **Machine owner** | The machine's owner, or an admin of its organization where it says so. |

Every protected path requires authentication, including agent session paths and loopback. Exceptions are `/health` and `/api/auth/{status,login,callback,refresh,logout,accounts,switch}` (the last two read and change only the browser's own sign-ins).

- Refused credentials get 401; forbidden origins/roles get 403; issuer failures get 503.
- A WebSocket refused at the door closes with 4400 plus the HTTP status's last two digits — 4401, 4403, and 4403 for an unavailable issuer (503) too — and closes 4410 when an established credential needs renewal.
- The fleet socket, machine tunnels, HTTPS tunnels (`/api/proxy/tunnel`) and terminal hosts take a renewed token over the open socket instead: a text frame `{"type": "token", "token": "<access token>"}`, accepted if it's valid and the same user's.
- When it rechecks an open connection the server reuses the issuer's answer about a token for up to 10 seconds (a new request always asks), and when the issuer can't be reached it keeps accepting a token it confirmed until that token expires.

Everything belongs to one organization (tenant): users, machines, providers, sessions and their records. No endpoint reaches another organization's things: they answer `404` as if missing, and lists leave them out. Site admins see and control everything in every organization, private sessions included. Every endpoint touching a session checks its sharing ([Accounts](accounts.md)); one you can't see answers `404`.

**Versions.** `agent` and `agent-machine` send `X-AI-Proxy-Version` and `X-AI-Proxy-Protocol` with every request; a protocol outside `MIN_PROTOCOL..PROTOCOL` gets `426 Upgrade Required` with how to update (WebSockets close `4426`). Requests without them, such as browsers', aren't checked.

## Agent-facing APIs

Agents call these paths on their loopback agent-proxy, which signs everything upstream with `X-AI-Proxy-Token` and keeps the vendor `Authorization`. Session paths and `X-AI-Proxy-Session` (native `/api/*` calls included) need an open session the caller may control. An unscoped signed write can create a private implicit session; an unscoped read never does, and a backend protocol error is refused before one is made. Implicit sessions are kept seven days when the organization's session retention is unlimited, within its minimum.

For non-site-admins, `/api/status` reports the request's public origin and address instead of the configured bind address, without backend URLs. `/api/models` lists usable models without backend URLs, versions, errors and runtime details unless the caller can view the machine. `/v1/models` answers `502` when Ollama model discovery fails; hosted Anthropic backends list their configured aliases. Refused interception audit events carry the trusted HTTP connection address, or the socket peer for native connections.

| Method | Path | What it does |
|---|---|---|
| GET, POST | `/v1/{path}`, `/session/{session_id}/v1/{path}` | OpenAI: chat completions, responses, embeddings and the rest. `GET .../models` lists the backend's models. On an Ollama backend, chat completions are translated to native `/api/chat` with the model's profile applied; Strata, Docker Model Runner and llama.cpp get them unchanged. An `api` backend gets `chat/completions`, `completions`, `embeddings` and `responses` only in the protocols it speaks (`400` otherwise), with its key and model aliases; its `models` lists its aliases and, with `discover`, its own. |
| POST | `/anthropic/v1/messages`, `/session/{session_id}/anthropic/v1/messages` | Anthropic Messages: translated for Ollama, passed through to Anthropic, Strata, Docker Model Runner, llama.cpp and an `api` backend that speaks `anthropic`. |
| POST | `/anthropic/v1/messages/count_tokens`, `/session/{session_id}/anthropic/v1/messages/count_tokens` | Token counting: passed through where the backend has it, estimated for Ollama. |
| GET, POST, PUT, PATCH, DELETE | `/anthropic/{path}`, `/session/{session_id}/anthropic/{path}` | Any other Anthropic path, forwarded only with a hosted `anthropic` backend. |
| GET, POST, DELETE | `/api/{path}`, `/session/{session_id}/api/{path}` | Native Ollama, for what agents call only: `chat`, `generate`, `embed`, `embeddings`, `tags`, `show`, `ps`, `version`. Profiles apply to `chat` and `generate` on Ollama; Docker Model Runner's Ollama API gets them unchanged; Strata and llama.cpp have none. Anything else (`pull`, `delete`, ...) answers `404`: models are managed through their machine. Agents use the session path, as the `agent-proxy` keeps unscoped `/api/...` for the launcher. |
| GET, POST, DELETE | `/session/{session_id}/mcp/{machine_id}/{name}` | A shared MCP server, as Streamable HTTP, relayed to its machine through its tunnel. Needs the `key` query parameter from the URL only the launcher gets (the session record has none), and only the servers the session started with, while its starter still holds the machine's `mcp` grant. Each POST is recorded in the session with its reply. See [MCP](mcp.md). |

Account agents don't use these: their HTTPS goes to the `agent-proxy`'s HTTPS proxy (`HTTPS_PROXY=http://<session>:agent-proxy@127.0.0.1:8183`), which carries each connection to `WS /api/proxy/tunnel` (below). Destinations that resolve to loopback, private, link-local or reserved addresses are refused.

The catch-alls here are registered after every console route, so `/api/{path}` never shadows one.

A non-streaming upstream reply cut off after its headers answers `502` and is recorded as failed; its connection and in-flight entry are released, so the session can be cleared or deleted. A cancelled buffered request is released too and recorded as `client_closed`.

## Health and the server

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/health` | Public | `status`, the running instance's id, the server's `version`, `protocol` and `min_protocol`. |
| GET | `/ready` | Public | Whether to send this process traffic: `ready`, `pod`, `roles` and `checks` (`database`, `cluster`, `blobs`, each `ok` and `detail`). `503` when one fails or the process is shutting down. See [Running several server processes](scaling.md#health-readiness-and-metrics). |
| GET | `/metrics` | The internal port only (`AI_PROXY_INTERNAL_PORT`), no token | Prometheus metrics of this process. Every other path on the internal port needs `AI_PROXY_INTERNAL_TOKEN`; the public port answers `/internal/*` with `404`. |
| GET | `/api/status` | Signed in | Totals, requests in flight, recent requests (the last 100; `?recent=0` leaves them out, for polling), intercept state, `default_backend`, whether the listen address needs a restart, and each machine backend's `provider`, counting only sessions you can see and backends your organization's agents may use. |
| GET | `/api/config` | Site admin | The editable network settings (`proxy.listen_host`, `listen_port`, `url`, `public_url`, `default_backend`, `backends`) and `restart_required`. |
| PUT | `/api/config` | Site admin | Stores them. Can remove local backends and add or edit hosted ones; refuses new or moved local ones. Without `backends`, leaves them alone; without `proxy.public_url`, leaves it as it is (`null` clears it). See [Configuration](configuration.md). |
| GET | `/api/config/export` | Site admin | Every setting as one YAML document (`proxy`, `default_backend`, `intercept`, `retention_days`, `request_log`, `backends`, `models`, `agents`); `?format=json` for JSON. |
| POST | `/api/config/import` | Site admin | Stores a settings document (YAML or JSON body): the parts it has, or every part with `?replace=true`, all checked first. Answers `changed` and `restart_required`. |
| GET | `/api/config/agents` | Site admin | Every stored agent: `id`, `definition`, `yaml` (the definition as YAML, to edit), `builtin` (it ships with the server) and `edited` (changed since). |
| PUT | `/api/config/agents/{agent_id}` | Site admin | Adds or changes an agent from its definition (YAML or JSON); a shipped one stops following the server's updates. `400` with the reason when invalid. |
| DELETE | `/api/config/agents/{agent_id}` | Site admin | Removes an agent (`204`); a shipped one stays removed through updates until added again or reset. |
| POST | `/api/config/agents/{agent_id}/reset` | Site admin | Resets a shipped agent to this version's definition, following updates again (`404` for others). |
| GET | `/api/config/models` | Site admin | Ollama model tuning: `defaults`, `profiles`, `aliases`. `?format=yaml` for YAML. |
| PUT | `/api/config/models` | Site admin | Stores the model tuning document (YAML or JSON body). |
| GET | `/api/config/providers` | Site admin | Every organization's API providers (`api` backends): each one's [settings](providers.md#settings), `tenant` (its organization), `has_key` and `key_source` (`vault`, `env` or `null`; never the key), `health` (what this process last saw) and `default`. Also `vault` (a key can be stored), `presets`, `protocols`, `auth_modes` and `share`. See [API providers](providers.md#api-providers-openai-and-anthropic-compatible). |
| PUT | `/api/config/providers/{name}` | Site admin | Adds or replaces one, from the settings above plus `key` (stored encrypted, write-only; needs `AI_PROXY_VAULT_KEYS`) and `clear_key`; `org` names the organization a new hosted one belongs to. Without `key` a stored key stays, unless the address moved to another origin. A machine-hosted one keeps its machine's address. `400` with the reason when a setting isn't allowed (private addresses, credential headers, `key_env` outside `AI_PROXY_API_KEY_*`, a team of another organization, ...). |
| DELETE | `/api/config/providers/{name}` | Site admin | Removes one and its stored key. `400` for the default backend. |
| POST | `/api/config/providers/{name}/test` | Site admin | Asks the provider for its models with its key: `ok`, `status`, `latency_ms`, `models` (ids, or `null`) and `error`. The result becomes its health and its discovered models. |
| GET | `/api/orgs/{org}/providers` | Admin of that organization, site admins | The organization's API providers (hosted ones added in it, and those on its machines), as above. `share` says whether you may set `orgs`. Others get `404`. |
| PUT | `/api/orgs/{org}/providers/{name}` | Admin of that organization, site admins; Same origin | Adds or changes one of its providers, as `/api/config/providers/{name}`. A new one belongs to this organization. `teams` are its own teams (`<org>/<team>`; empty is everyone in it); `orgs` is kept as it was unless a site admin sends it. `fallback` names only its own providers. A name another organization uses answers `409`. |
| DELETE | `/api/orgs/{org}/providers/{name}` | Admin of that organization, site admins; Same origin | Removes one of its providers and its stored key. |
| POST | `/api/orgs/{org}/providers/{name}/test` | Admin of that organization, site admins; Same origin | Tests one of its providers, as `/api/config/providers/{name}/test`. |
| GET | `/api/config/storage` | Site admin | `database` (`kind`: `sqlite` or `postgresql`, `url` with its password hidden, `size` in bytes), `retention_days` and `request_log` (`enabled`, `days`, `payloads`, `payload_days`). |
| PUT | `/api/config/storage` | Site admin | `{"retention_days": days}` (1 to 36500, or `null` to keep recordings until deleted) and/or `{"request_log": {...}}` with any of `enabled` and `payloads` (true or false), `days` (1 to 3650) and `payload_days` (1 to 365); the others stay. Old recordings go at once. Answers as GET. |
| GET | `/api/server/stats` | Site admin | The server computer's load and disk use, read when asked (nothing is saved): `hostname`, `platform`, `boot_time`; `cpu` (`percent`, `count`, `load_average`, null on Windows); `memory` and `swap` (bytes, `percent`); `process` (agent-server's `pid`, `rss`, `cpu_percent` of one core, `threads`, `started_at`); `disks`, each filesystem holding the log or build folder once (`mountpoint`, `total`, `used`, `free`, `percent`, `folders`); `folders` (`name`, `path`, `bytes`) and `database` (`kind`, `bytes`), measured in the background every five minutes, so `bytes` is null until `folders_measured_at` is set. |
| GET | `/api/server/access-log` | Site admin | Every HTTP request and WebSocket received since the server started, newest first (from memory). `?failed=true` keeps failures and dropped WebSockets only; `q` matches words in the path, address, user, user agent, status and reason; `limit` (1 to 1000, default 200); `before` (an `id`) pages back. Each entry: `id`, `time`, `kind` (`http`, `websocket`), `method` (`WS` for WebSockets), `path`, `query` (parameter names only), `client`, `forwarded_for`, `host`, `user_agent`, `program_version`, `protocol`, `user`, `tenant`, `status` (101 for an accepted WebSocket), `close_code`, `close_reason`, `closed_by` (`server`, `client`), `detail` (the start of an error's body), `machine_id` and `machine_name` (a fleet connection's, once its hello named it), `duration_ms`, `outcome` (`ok`, `failed`, `dropped`, `open`). Also `started_at`, `totals` (`ok`, `failed`, `dropped`, `open_websockets`) and `kept` (`all`, `failed`). |
| GET | `/api/server/access-log/stats` | Site admin | Since the server started: `totals`; `per_minute` (the last 60 minutes: `minute`, `ok`, `failed`, `dropped`); `answers` (`answer`, `count`: a status, or how a WebSocket ended, like `4401 by server` or `refused 403, 4401`); `durations` (`count`, `p50`, `p95`, `max` in ms, of the newest 2,000 HTTP answers); `failing_routes` (`method`, `route` with ids as `{id}`, `answer`, `count`, and the last one's `time`, `user`, `user_agent`, `program_version`, `reason`); `failing_clients` (`address`, `count`, the same last fields); `open_websockets` (`path`, `user`, `machine_id`, `machine_name`, `address`, `since`, `user_agent`, `program_version`). And `saved`, from the database: `days` (the last 14: `day`, `ok`, `failed`, `dropped`), `fleet` (each machine's connections over `fleet_days`, 7: `machine_id`, `machine_name`, `user`, `address`, `connections`, `ok`, `failed`, `dropped`, `connected_ms`, `last_time`, `last_answer`, `last_reason`, `user_agent`, `program_version`), `oldest`, `count`, `kept_days`. |
| GET | `/api/server/access-log/history` | Site admin | What's saved, across restarts, newest first: failures, dropped WebSockets and every fleet connection (entries as above, with `saved: true` and a string `id`). `failed=true`, `fleet=true` (fleet connections only), `q`, `limit` (1 to 1000), `before` (a `time`) pages back. Also `kept_days`. |
| GET | `/api/server/request-log` | Site admin | The API log: every saved request, newest first. `q` matches words in the path, query names, address, user, machine, user agent, status and reason; `method` (`GET`, `POST`, ..., `WS`); `status` (`2xx`, `3xx`, `4xx`, `5xx`, `failed`, `websocket`; `400` for anything else); `user`; `limit` (1 to 500, default 100); `before` and `since` (times). Entries as the access log's, with a string `id` and `payload` (debug mode saved its headers and bodies). Also `settings` (the `request_log` setting). |
| GET | `/api/server/request-log/summary` | Site admin | `count` and `oldest` of the saved requests, `payloads` and `oldest_payload` of their saved payloads, and `settings`. |
| GET | `/api/server/request-log/{id}` | Site admin | One saved request, and `payloads`: `request` (`query` with secret-named values replaced, `headers`, `body`) and `response` (`headers`, `body`), or null when debug mode was off or the payload has expired. A `body` has `bytes`, `truncated` (only the first 64 KB is kept), and `json`, `text` or `omitted` (a binary body's type). Credential headers and secret-named fields read `[redacted]`. `404` when it's gone. |
| POST | `/api/server/restart` | Site admin | Restarts with the stored settings once in-flight requests finish (up to 30 s). Answers `202` with `waiting_for` and the address it comes back on. `409` when the server can't restart itself (`--reload`); `401` from an untrusted or cross-origin caller. |

## Sessions and requests

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/api/sessions` | Signed in | Opens a session for other tools: `client`, `project`, `model`, `backend` (or `direct`), `trace`, `tags`, and sharing: `visibility`, `team`, `org`, `access`. A `shell` client is always private. |
| GET | `/api/sessions` | Signed in | The sessions you can see, each with its `machine` and what `you` may do. |
| DELETE | `/api/sessions` | Signed in | Deletes all saved traffic: ended sessions and ones idle 15 minutes go, recently active ones keep running without their data, ones with requests in flight are skipped. Only your own (admins: also their organization's ownerless ones). |
| GET | `/api/sessions/{session_id}` | Signed in, can view | One session. |
| PATCH | `/api/sessions/{session_id}` | Signed in, can control | Ends it (`ended`, `exit_status`), switches capture (`trace`; `409` when its organization's [retention policy](retention.md) records every session, or none), or for its owner changes its sharing (`visibility`, `team`, `org`, `access`). Narrowing the sharing drops terminal viewers who lose access. A shell can't be shared. |
| POST | `/api/sessions/{session_id}/stop` | Signed in, can control | Stops replies the server is still reading for a client that dropped (Ask a model's **Stop**; ending the session does too): `{"stopped": <count>}`. |
| DELETE | `/api/sessions/{session_id}` | Signed in, can control | Deletes it. `409` while requests are in flight. |
| DELETE | `/api/sessions/{session_id}/data` | Signed in, can control | Clears its saved requests and traces, keeping the session. |
| GET | `/api/requests` | Signed in | Requests you can see, newest first, with `trace_available`. `?session_id=` for one session, `?model=` and `?kind=` to filter, `?limit=` (1 to 5000) and `?before=` (a timestamp: only older ones) to page. Without `limit`, all of them. |
| GET | `/api/sessions/{session_id}/telemetry` | Signed in, can view | The session's request records as a download (newline-delimited JSON, `<id>-requests.jsonl`). |
| GET | `/api/sessions/{session_id}/traces` | Signed in, can view | The names of its saved traces. |
| GET | `/api/sessions/{session_id}/traces/{request_id}` | Signed in, can view | One trace: the full request and response. |
| GET | `/api/sessions/{session_id}/chat` | Signed in, can view | The session's traced model calls read back as chats: its threads (each with `kind`: `main`, `conversation`, `subagent` or `background`, and a background call's `purpose`), and a page of one thread's turns. Each turn has `at` (when it was sent); text sent as the user has `author` (`person`, `parent`, `agent` or `unknown`) and, for the agent's own, `note`. `inherited` counts the turns at the start a sub-agent or background call was handed. `?thread=`, `?before=`, `?limit=` (1 to 500, default 60). |
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
| GET | `/api/agents` | Signed in | Every agent the server stores, with the `backends` it can use (your organization's and the shared hosted ones) and the `default_backend` a launch picks (`direct` for account agents). |
| POST | `/api/agents/{agent_id}/sessions` | Signed in | Opens a session for an agent and returns `session`, the rendered `env`, `command` and `files`, `container` (whether its backend is a container to start first), `mcp` (the shared MCP servers it got, with their URLs) and `shell` (the same as bash and PowerShell lines). Takes `model`, `backend`, `trace`, `project`, `project_path`, `mcp`, sharing, `tags`; the launcher adds `launcher: true`, its `proxy_url`, its `intercept` paths and `files_dir`. `409` for an account agent when capture is off. |
| GET | `/api/models` | Signed in | The models on each backend your organization's agents may use (or `?backend=` for one; another organization's answers `404`), asked of the backend's machine, with the profile the server applies, plus the `aliases` from its Ollama model tuning. |
| GET | `/api/mcp` | Signed in | The MCP servers machines share that you may give agents. |

## HTTPS capture

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/intercept` | Signed in | Whether capture is running, its settings, and the CA's path and fingerprint. |
| GET | `/api/intercept/ca.pem` | Signed in | The capture CA's certificate. `404` when capture isn't running. |
| GET | `/api/intercept/bundle.pem` | Signed in | Common root certificates plus the CA, for tools that take one CA file. `404` when capture isn't running. |

## Machines and their providers

Every machine route needs **Trusted**; a machine you may not see answers `404`. Everything about a model server is asked of its machine over the fleet socket ([Machines](machines.md), [Providers](providers.md)). A model server is named `{kind}-{port}`, such as `ollama-11434`, `llamacpp-9931`, `strata-18500` or `docker-12434`.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/machines` | Trusted | The machines you can see (connected first), the ones you removed, and the list of `features`. |
| GET | `/api/machines/{machine_id}` | Trusted, `view` | One machine: what it runs and reports, its backends, launch history filtered by session access (plus your own pending/failed launches), and what `you` may do. |
| DELETE | `/api/machines/{machine_id}` | Owner or admin, Same origin | Removes it from the fleet, connected or not. It's refused when it connects again until restored. |
| POST | `/api/machines/{machine_id}/restore` | Owner or admin, Same origin | Lets a removed machine join again. |
| PUT | `/api/machines/{machine_id}/access` | Owner or admin, Same origin | Sets who in the machine's organization may use each feature: `{"access": {feature: {"orgs": [<its organization>], "teams": ["<org>/<team>", ...]}}}`. Another organization or its teams are refused (`400`). Members grant only teams they're in. |
| POST | `/api/machines/{machine_id}/launch` | `launch`, Same origin | Starts an agent there in a shared terminal: `agent`, `project` (inside one of its roots), `model`, `backend`, `trace`, `visibility`, `team`, `access`, `mcp`, and for account agents `account_id` (one of your [subscriptions](subscriptions.md) assigned to the machine, or `auto`). Needs `agent-machine launches on`. Answers `201` once the session is open, with the `account` it runs with; someone other than the owner is tagged `launched_by`. An account agent started by anyone but the machine's owner needs an `account_id` they may use (`403` without one); one that can't take new work answers `409`. |
| POST | `/api/machines/{machine_id}/shell` | `terminal`, Same origin | Opens a shell there in a private terminal: `project` (a folder in its roots; default the first root). Needs `agent-machine commands on`. |
| POST | `/api/machines/{machine_id}/update` | `update`, Same origin | Has the machine update `agent-machine` (and with `"agent": true`, `agent`) to the server's build, then restart. Needs `agent-machine updates on`. |
| GET | `/api/machines/{machine_id}/folders` | `launch` or `terminal` | Folder names inside one of its launch folders, `?path=`. Without a path, its roots. |
| GET | `/api/machines/{machine_id}/files` | `files` | The folders and files inside one of its launch folders, `?path=`: `path`, `parent` (null at a root), `entries` (`name`, `path`, `kind` `folder` or `file`, `size` for files, `modified` in seconds), `truncated` (more than 1000). Without a path, its roots. Hidden names are left out. 409 without `agent-machine files on`; 400 for a path outside the roots or hidden. |
| GET | `/api/machines/{machine_id}/files/content` | `files` | One file's text, `?path=`: `path`, `name`, `size`, `modified`, `binary`, `truncated` (longer than 1 MB: only the start is sent) and `content` (null when binary). The same checks as `/files`. |
| POST | `/api/machines/{machine_id}/providers/adopt` | Admin | Adds a model server the machine runs as a backend, for its organization's agents only: `{"type": "ollama"}`. `{"type": "api", "port": 8000}` names one of several OpenAI-compatible servers by its port; an `api` backend gets `<address>/v1` with Chat Completions only and no key. A matching legacy backend without a machine (the root's) is linked in place, only for a machine in the root, retaining its name and settings. `409` if it's already one of this machine's backends. Another machine's backend at the same address doesn't count: machines behind one public address (a PC and its WSL, a home router) look alike to the server, and agents reach each one through its own machine. |
| GET | `/api/machines/{machine_id}/providers/{provider_id}` | `view` | The model server as the machine sees it now (models, what's loaded, status), its backend, and the traffic agents sent it. |
| POST | `/api/machines/{machine_id}/providers/{provider_id}/models/{action}` | `models`, Same origin | `pull`, `delete`, `load` or `unload` a model: `{"name": ...}`. Strata only loads and unloads. llama.cpp pulls a Hugging Face model named `user/repo[:quant]`; its `pull` takes `method`: `""` (default: the router, else agent-machine when the router can't), `"router"` or `"direct"` (agent-machine downloads the files into its GGUF folder). llama.cpp also has `files` (`{"name": ...}`: the model's files, their URLs, sizes and SHA-256, the GGUF folder, and `serves_folder`) and `reload` (the router reads its GGUF folder again). `pull` streams the machine's progress as NDJSON lines, ending with `{"status": "success"}` or `{"status": "error"}`. |
| GET | `/api/machines/{machine_id}/providers/{provider_id}/log` | `view` | The end of its log on the machine: `?source=server` or `engine` (Strata), `?tail=` bytes. Ollama's and llama.cpp's are the ones agent-machine started. |
| GET | `/api/machines/{machine_id}/metrics` | `view` | The machine's CPU, memory and GPU load, sampled there. |
| GET | `/api/machines/{machine_id}/catalog/{catalog}` | `view` | Searches a model catalogue from the machine: `ollama`, `huggingface`, `huggingface-quants`, `docker-hub` or `docker-hub-tags`, with `?q=` and `?repo=`. |
| GET | `/api/machines/{machine_id}/servers/ollama` | `view` | The settings `agent-machine` starts the machine's Ollama with, and its state. |
| PUT | `/api/machines/{machine_id}/servers/ollama` | `servers`, Same origin | Saves them; they apply when Ollama next starts. Needs `agent-machine servers on`. |
| GET | `/api/machines/{machine_id}/servers/llamacpp` | `view` | The `LLAMA_ARG_*` settings `agent-machine` starts the machine's llama-server with, and its state (installed, running, an install's progress). |
| PUT | `/api/machines/{machine_id}/servers/llamacpp` | `servers`, Same origin | Saves them; they apply when llama.cpp next starts. Needs `agent-machine servers on`. |
| GET | `/api/machines/{machine_id}/servers/llamacpp/install` | `servers` | How llama.cpp can be installed there, as the machine finds out: `source` (`termux` or `github`), `release`, and `variants` (`id`, `label`, `help`, `size`, `recommended`). |
| POST | `/api/machines/{machine_id}/servers/{kind}/{action}` | `servers`, Same origin | Starts, stops or restarts the machine's Ollama (`kind` `ollama`, `action` `start`, `stop` or `restart`). For `kind` `strata`: `start`, `stop` or `remove` a profile (`{"profile": ...}`; `remove` only deletes a profile whose model is gone and that isn't serving), `prune` (remove every such profile) or `cleanup` (`strata runtime cleanup`: leftovers of runtimes whose owner is gone). For `kind` `llamacpp`: `start`, `stop`, `restart`, `install` (`{"variant": ...}`, one of the install options; it answers at once and the machine's state carries the progress) or `uninstall` (the builds installed from the console). Needs `agent-machine servers on`. |

## Provider accounts (subscriptions)

People's own AI plans and where they run ([Subscriptions](subscriptions.md)). All need **Signed in**, changes also **Same origin**. Answers carry metadata only, never a token except in the one-time handover; another person's subscription answers `404` unless you may see it.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/provider-accounts` | Signed in | The subscriptions you may see (yours, those you may launch with, and for admins their organization's), with their state and machines; `?machine=` only those assigned to that machine. Also `providers`, `modes`, `states`, `windows` and whether the vault is set (`vault.configured`). |
| POST | `/api/provider-accounts` | Signed in | A new one of yours: `provider` (`claude`, `codex`, `copilot`), `display_name`, `plan_name`, `authentication_mode` (`runner`, or `token` for Copilot), and any `PATCH` field. `201`; the one you already have with that provider and name answers `200` with `existing: true`. |
| POST | `/api/provider-accounts/plan` | Signed in | Which subscription, agent and machine should take a new task, best first, and why others can't: `agents` (account agents in order), `machine`, `project`, `fallback` (local-model agents only), `allow_unknown`. Answers `choice`, `candidates`, `skipped`. |
| GET | `/api/provider-accounts/{id}` | May see it | One, with its `usage`. |
| PATCH | `/api/provider-accounts/{id}` | Owner; `status`: owner or admin | `display_name`, `plan_name`, `priority`, `users` (others who may launch with it), `limit` (`{window, unit, value}`, or null), `warning_percent`, `stop_percent`, `clear_observations`; `status` `active` or `disabled`. |
| DELETE | `/api/provider-accounts/{id}` | Owner | Removes it, its assignments, usage and stored token. |
| GET | `/api/provider-accounts/{id}/health` | May see it | Its state and each assigned machine's health. |
| GET | `/api/provider-accounts/{id}/usage` | May see it | `observed` usage by window, the `estimate` against a declared limit (or `remaining: "unknown"`), `rate_limits` seen, `reported` (none yet). |
| GET | `/api/provider-accounts/{id}/events` | Owner or admin | Its history: created, enrolled, assigned, credential stored, provisioned, revoked, rate limited, ... |
| GET | `/api/provider-accounts/{id}/assignments` | May see it | The machines it may run on, each with its health. |
| PUT | `/api/provider-accounts/{id}/assignments/{machine_id}` | Owner | Lets it run on one of your own machines: `enabled`, `provisioning_allowed` (a token account's token may go to launches there). Another person's machine is `403`. |
| DELETE | `/api/provider-accounts/{id}/assignments/{machine_id}` | Owner | Stops it running there; new launches are refused. |
| POST | `/api/provider-accounts/{id}/enrollments` | Owner | Registers a machine as one that runs it (`agent accounts enroll`): `machine`, else `hostname` finds your machine of that name; `detected` (`auth`, `version`). `201`. |
| PUT | `/api/provider-accounts/{id}/credential` | Owner | Stores its managed token, encrypted: `{"token": "github_pat_..."}`. `409` without `AI_PROXY_VAULT_KEYS`; `400` for a token Copilot CLI doesn't take. Never readable back. |
| POST | `/api/provider-accounts/{id}/revoke` | Owner | Forgets its token and stops handing it out. |
| POST | `/api/sessions/{session_id}/provider-credential` | The session's owner | The managed token for a session the server launched on an assigned machine, once, within two minutes: `{"env": {"COPILOT_GITHUB_TOKEN": "..."}}`, `Cache-Control: no-store`. `403` when it was handed over already, expired or isn't allowed there. |

`POST /api/agents/{agent_id}/sessions` takes `account` too: the session is tagged `provider_account` (a server tag clients can't set) after the same checks, and the answer's `account` says whether a token waits (`credential: true`).

## Providers and usage limits

Each person's usage of each provider (a backend, or a kind of subscription) and the token limits an organization sets on it. See [Providers and usage limits](usage-limits.md).

| Method | Path | Who | What |
| --- | --- | --- | --- |
| GET | `/api/providers` | Signed in | The providers your organization can use (`backend:<name>` for model servers and hosted APIs, `subscription:<claude\|codex\|copilot>`), each with your `usage` in every window, the `limits` on you and `blocked`. |
| GET | `/api/orgs/{org}/usage` | Its members (their own rows), its admins and site admins (everyone's) | Usage by person and provider in every window, with each row's `limits` and `blocked`; for admins also the organization's `limits` (`manage: true`). |
| PUT | `/api/orgs/{org}/limits` | Admin of that organization, site admins | One limit: `user` (a person in it, or `*` for everyone), `provider`, `window` (`5h`, `24h`, `7d`, `30d`, `month`), `tokens` (0 or more; 0 turns it off). Answers the usage report. |
| DELETE | `/api/orgs/{org}/limits?user=&provider=&window=` | Admin of that organization, site admins | Removes one limit. |

A person at a limit gets `429` (`error.code: "usage_limit"`, `Retry-After`) from the agent APIs (`/v1`, `/anthropic`, `/api` model calls, and account agents' calls to the subscription's hosts), and from `POST /api/agents/{agent_id}/sessions` and `POST /api/machines/{id}/launch` for that provider.

## Retention and the audit log

What's recorded and for how long, legal holds, and the audit log. See [Retention](retention.md) and [The audit log](audit.md).

| Method | Path | Who | What |
| --- | --- | --- | --- |
| GET | `/api/config/retention` | Site admin | The server's policy: `capture` (`traces`: `off`, `opt_in` or `always`; `intercept_bodies`; `terminal_output`), `allowed_traces`, and `days`, `max` and `min` for `traces`, `terminal`, `records`, `sessions` and `audit` (days, or `null` for forever). |
| PUT | `/api/config/retention` | Site admin | Stores it (the whole policy). `400` with the reason when it isn't consistent. Every session's next deletion is worked out again. |
| GET | `/api/orgs/{org}/retention` | Its members, its admins, site admins | The policy its sessions start under (`capture`, `days`), what it sets itself (`own`), the server's policy and bounds (`server`: `capture`, `days`, `allowed_traces`, `max`, `min`) and how many `holds` it has. |
| PUT | `/api/orgs/{org}/retention` | Admin of that organization, site admins | Its own policy: `capture` and `days`, only what it changes. `400` outside the server's bounds. |
| DELETE | `/api/orgs/{org}/retention` | Admin of that organization, site admins | Back to the server's policy. |
| GET | `/api/holds` | Admins (their organization's), site admins (all, or `?org=`) | Legal holds: `id`, `tenant`, `scope` (`org`, `user`, `session`), `target`, `reason`, `set_by`, `set_at`. |
| POST | `/api/holds` | Admin of the organization, site admins | Places one: `scope`, `target`, `reason`, and `org` (default: yours). Nothing it covers is deleted until it's lifted. |
| DELETE | `/api/holds/{id}` | Admin of its organization, site admins | Lifts it. |
| GET | `/api/audit` | Admins (their organization's), site admins (everyone's, or `?org=`) | Events, newest first: `type` (a prefix such as `machines` or `mesh.audit.data.viewed`), `actor`, `outcome`, `since`, `before` (seconds since the epoch), `limit` (up to 1000). Answers `events` and `next_before`, a cursor to pass as `before` for the next page (`null` on the last). Members get `403`. |
| GET | `/api/audit/export` | The same | The same events (up to 5000) as `?format=ndjson` (default) or `csv`, as a download. |
| GET | `/api/audit/verify` | The same | Each chain recomputed and checked against the checkpoints: `ok`, `tenants` (`events`, `first`, `head`), `checkpoints`, `problems`. |
| POST | `/api/audit/checkpoint` | Site admin | Signs every chain's head now; `checkpoint` is `null` when nothing was recorded since the last one. |
| GET | `/api/config/audit` | Site admin | `exports` (`name`, `kind`: `otlp`, `syslog`, `webhook` or `blob`, `url`, `token_env`, `tenants`, `enabled`, `prefix`), `checkpoint_minutes`, the audit key's `key_id` and `public_key`, and the `backlog`. |
| PUT | `/api/config/audit` | Site admin | Stores `exports` and `checkpoint_minutes` (5 to 1440). |

## Containers

Containers the fleet's machines run as backends. See [Machines](machines.md). Changes also need **Same origin** (a cross-origin or untrusted change answers `401`).

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/containers` | Trusted | The machines you can see that could run containers (their CLI, whether it answers, the engines they offer), and the state of the containers on them. Another organization's containers answer `404` everywhere. |
| GET | `/api/containers/{name}` | Trusted | One container's state. |
| POST | `/api/containers` | A site admin, or an admin who owns the machine | Creates one on a connected machine with `agent-machine containers on`: `name`, `machine`, `engine`, and `image`, `port`, `gpus`, `env`, `volume`. Saves it as a backend. |
| PUT | `/api/containers/{name}` | A site admin, or an admin who owns the machine | Changes its settings; they apply when it next starts. The engine can't change. |
| DELETE | `/api/containers/{name}` | A site admin, or an admin who owns the machine | Removes it from its machine and from the server's backends. |
| POST | `/api/containers/{name}/{action}` | Trusted, sees the machine | `start`, `stop` or `restart`. Answers `202` with the job. `409` with `conflicts` when other containers hold the GPU it needs; send `{"switch": true}` to stop them first. |
| GET | `/api/containers/{name}/log` | Trusted, sees the machine | Its container log, `?tail=` lines (10 to 5000, default 400). |

## Terminals and pairing

Shared terminals are a remote shell, so these routes need **Trusted** and **Same origin** (except the fixed answers below: `/api/remote*` and the removed pairing stubs answer the same to anyone signed in). See [Accounts](accounts.md).

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/remote` | Signed in | `{trusted: true, local: false, paired: false, accounts: true}`, for launchers checking they may share a terminal (no access key). |
| POST | `/api/remote/code`, `/api/remote/rotate` | Signed in | Always 403: the access key and pairing codes were removed. |
| POST | `/api/remote/pair` | Signed in | Always 410: pairing was removed; sign in through OIDC. |
| POST | `/api/remote/unpair` | Signed in, Same origin | Removes only the legacy pairing cookie; use `/api/auth/logout` to sign out. |
| GET | `/api/terminals` | Trusted | The shared terminals you can see, ended ones included (`?open=1`: only those whose terminal and session are still running): agent, machine, project, model, backend, state, activity, requests in flight and `attention` (waiting, with the question on screen). |

The two terminal WebSockets are below.

## Accounts, organizations and users

See [Accounts](accounts.md). Changes answer with the directory (`orgs` and `users`) as you may see it. Authentication is always required.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/auth/status` | Public | Always `required: true`; current identity, HTTPS, issuer, audience, CLI client id, `redirect_uris` (the browser sign-in's registered callbacks; the Android app keeps sign-in in its WebView on their hosts), `public_url` (the console's address for everyone else, `proxy.public_url`, or null; `agent-tray` opens it), `expires_in` (seconds left on the access token the request carried, or null) and `access_token` (that token, when it came from the browser's cookie: the console sends it over its open sockets to renew them). |
| GET | `/api/auth/login` | Public | Starts PKCE browser authorization; `return_to` must match an allowed console origin. |
| GET | `/api/auth/callback` | Public | Validates OIDC state, code exchange and nonce; adds the account to the browser's sign-ins and makes it active. A sixth account goes back with `?sign_in=too_many`. |
| POST | `/api/auth/refresh` | Public, Same origin | Rotates the active account's refresh token and renews the access cookie. Answers `{"refreshed": true, "expires_in": <seconds>, "access_token": <the new token>}`; `/api/auth/switch` adds `expires_in` too. |
| GET | `/api/auth/accounts` | Public | The accounts this browser is signed in to: `active` (its name, or null), `accounts` (`user`, `active`), and `max` (how many one browser holds, 5). Names only; tokens never leave the cookie. |
| POST | `/api/auth/switch` | Public, Same origin | `{"user"}`: makes another of this browser's accounts the active one, with a fresh access cookie. No password. `404` for an account it isn't signed in to; `401` when that sign-in has ended (it's dropped). |
| POST | `/api/auth/logout` | Public, Same origin | Signs out of the active account, or `{"user": name}`, or every one with `{"all": true}`, revoking each at the issuer. When the active one goes, the next one left becomes active. Answers the accounts left. |
| POST | `/api/auth/device` | Signed in (console), Same origin | Approve an `agent login`/`agent-machine login` code as yourself, without a password: `{user_code, action}`. `check` answers what asked (`client`, `requested_at`, `expires_at`, `user`) and decides nothing; `approve` or `deny` answers `{approved}`. `404` wrong or expired code, `409` already decided, `403` not a console sign-in, `429` too many tries. |
| GET | `/api/auth/setup-codes` | Signed in (console) | Your open setup codes: `{codes: [{id, label, created, exp, uses, max_uses}]}` (times in seconds since the epoch; `uses` is sign-ins left). Never the codes themselves. |
| POST | `/api/auth/setup-codes` | Signed in (console), Same origin | Makes a setup code: `{lifetime, uses, label}` (lifetime 60 s to 24 h, default 1 h; uses 1 to 100, default 1). Answers `201` with the code (`msc_...`), once, and its listing fields. Each sign-in with it (`agent-machine login --code`, `AI_PROXY_MACHINE_SETUP_CODE`, `agent login --code`, `AI_PROXY_SETUP_CODE`) is yours, with nothing to approve. `409` 20 open already, `400` out of range, `403` not a console sign-in. |
| DELETE | `/api/auth/setup-codes/{id}` | Signed in (console), Same origin | Revokes one before it expires; what it already signed in stays signed in. `404` no such open code. |
| POST | `/api/auth/login` | Public | 410; password sign-in moved to the issuer. |
| POST | `/api/auth/token`, `/api/auth/password` | Signed in | 410; legacy password grants and account password changes removed. |
| GET, POST | `/api/auth/tokens` | Signed in | 410; launcher keys replaced by OAuth grants. |
| DELETE | `/api/auth/tokens/{token_id}` | Signed in | 410; launcher keys replaced by OAuth grants. |
| GET | `/api/orgs` | Signed in | Organizations with their teams and members (the users whose organization it is), each with `root` and `parent` (`"root"`, or `null` for the root itself). You see your own; site admins see every one, the root first. |
| POST | `/api/orgs` | Site admin | `{"id", "name"}`: adds an organization (a tenant, under the root). |
| PUT | `/api/orgs/{org}` | Admin | `{"name"}`: renames it. |
| DELETE | `/api/orgs/{org}` | Site admin | Removes an organization with no users left (`400` otherwise, also when one of its API providers is the default backend or it has legal holds). Its hosted API providers (with their keys), machines, usage limits and retention policy go with it, and its sessions become site-admin-only, so a recreated name inherits nothing. The root can't be removed. |
| PUT / DELETE | `/api/orgs/{org}/members/{name}` | Admin | Users can't join or leave organizations: `400`, except a `PUT` for the user's own organization, which changes nothing. |
| POST | `/api/orgs/{org}/teams` | Admin | `{"id", "name"}`: adds a team. |
| PUT | `/api/orgs/{org}/teams/{team}` | Admin | `{"name"}`: changes the display name, keeping its id and sharing key. |
| DELETE | `/api/orgs/{org}/teams/{team}` | Admin | Removes a team. |
| PUT / DELETE | `/api/orgs/{org}/teams/{team}/members/{name}` | Admin | Adds/removes one team membership, for a user of that organization. No body needed. |
| GET | `/api/users` | Signed in | Users with their `role`, `tenant` (their organization; `orgs` is `[tenant]`) and `teams`. You see the people in your organization; site admins see everyone. |
| POST | `/api/users` | Admin | `{"name", "password", "role", "tenant", "teams"}`: adds a user to one organization, for good. An organization's admin adds to their own (`tenant` may be left out); a site admin names any, and without one makes a site admin (`role: "admin"` only). `orgs: [one]` still works for `tenant`. |
| PUT | `/api/users/{name}` | Admin | Changes `role`, `teams` or `password` together, only if all changes are valid. `orgs` may only repeat the user's own organization. An organization's last admin can't be made a member; root users stay admins. |
| DELETE | `/api/users/{name}` | Admin | Removes a user. Their machines leave the fleet, their provider accounts (and stored credentials) are deleted, and their sessions become ownerless, kept for the organization's admins: a recreated name inherits nothing. You can't remove yourself. |

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
| GET | `/uninstall.sh` | The uninstaller for Linux, macOS and FreeBSD, the same file for every request and needing no build: `curl -fsSL <server>/uninstall.sh \| sh` (`-s -- --purge` also signs out and deletes settings). |
| GET | `/uninstall.ps1` | The same for Windows: `irm <server>/uninstall.ps1 \| iex`. |

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

OpenAPI can't describe these. All need **Trusted** (a sign-in; machines send their own token) and **Same origin**.

| Path | Who connects | What it carries |
|---|---|---|
| `/api/machines/connect` | `agent-machine run`, with its token | One machine in the fleet, for as long as it runs. In: its `hello` (id, name, installed agents, model servers, containers and engines, launch and server settings, shared MCP servers), then `update` every minute, `ping`, `token` (its new access token before the old one expires, so the server keeps the socket open; ignored unless valid and the same user's), and answers (`launched`, `launch_failed`, `container_result`, `server_result`, `provider_result`, `provider_progress`, `folders_result`, `files_result`, `update_result`). Out: `welcome`, then requests: `launch`, `shell`, `tunnel`, and `container`, `server`, `provider`, `folders`, `files` (`list` or `read`) and `update` requests with an `op`. A removed machine is closed with `4403`. |
| `/api/machines/tunnel/{tunnel_id}` | `agent-machine`, with its token, when the server asks | One connection of agents' traffic (or MCP traffic) to a port on that machine: raw bytes both ways. Closed with `4404` for an unknown tunnel. |
| `/api/proxy/tunnel` | `agent-proxy`, with its machine's token | One account agent's HTTPS connection, which the server decrypts and records with its CA. Binary frames both ways. Closed with `4404` when capture is off. |
| `/api/terminals/{session_id}/host` | The launcher (`--control`), relayed by its `agent-proxy` | The launcher sends a hello, then the agent's output as binary frames and `resize`, `gap` and `exit` messages. The server sends `resume` (where to resend from) and `input` (`parts` to type, sent a short gap apart). Only someone who can control the session may host it; a missing session closes `4404`, a credential that may not `4401`. |
| `/api/terminals/{session_id}/view` | The console's terminal page | `?from=<offset>` (optional): the byte offset of the output the viewer already has. The server sends `state`, `{"type": "replay", "start": <offset>, "resumed": <bool>}` (resumed: what follows continues from `from`, so nothing is redrawn), the history as binary frames, `replayed`, `gap` (`{"type": "gap", "to": <offset>}`: output the launcher skipped), then live output and `state`, `resize` and `error` messages. The viewer sends `{"type": "input", "text": "...", "enter": true}` or `{"type": "input", "keys": "\u001b"}`; typing needs control of the session, checked on every message. `{"type": "token", "token": "..."}` renews the viewer's access token in place (the same user's, in the same tenant; anything else is ignored), so it isn't closed with `4410` when the token it opened with expires. A viewer that falls behind is closed with `4408`, and a missing terminal with `4404`. A viewer whose access ends mid-stream (sharing narrowed, the session made private) is closed with `4403`, and `4410` also closes a viewer whose credential can't be rechecked because the issuer is unreachable. |

## Check

These tables cover every route on the public port (compare a test server's `app.routes`), except the `/assets` mount and the internal port's routes (`POST /internal/fleet/{machine_id}/{action}`, `GET /internal/live`, `GET /metrics`, `WS /internal/tunnels/{tunnel_id}`), which only the server's own processes reach ([Running several server processes](scaling.md)). Adding or removing a route changes its table in the same change.
