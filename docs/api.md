# API

Everything the server does is an HTTP or WebSocket API on its one port. The console uses it, and so do the launcher (`agent`), each machine's `agent-proxy` and `agent-machine`. This page lists every route the server registers.

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
| **Machine feature** | The fleet's grants, `fleet.allowed(identity, machine, feature)`, only ever inside the machine's organization: the machine's owner always; its organization's admins for `view`, `models` and `update`; the organization or teams the owner granted the feature. See [Machines](machines.md). |
| **Machine owner** | The machine's owner, or an admin of its organization where it says so. |

Every protected path requires authentication, including agent session paths and loopback. Exceptions are `/health` and `/api/auth/{status,login,callback,refresh,logout}`. Refused credentials get 401; forbidden origins/roles get 403; issuer failures get 503. WebSockets initially close 4401/4403 and close 4410 when an established credential needs renewal. The fleet socket, machine tunnels, HTTPS tunnels (`/api/proxy/tunnel`) and terminal hosts take a renewed token over the open socket instead: a text frame `{"type": "token", "token": "<access token>"}`, accepted if it's valid and the same user's. When it rechecks an open connection the server reuses the issuer's answer about a token for up to 10 seconds (a new request always asks), and when the issuer can't be reached it keeps accepting a token it confirmed until that token expires.

Everything belongs to one organization (tenant): users, machines, providers, sessions and their records. No endpoint reaches another organization's; they answer `404` as if missing, and lists leave them out. Site admins are the exception: they see and control everything in every organization, private sessions included. Every endpoint that touches a session checks its sharing (see [Accounts](accounts.md)). A session you can't see answers `404`.

**Versions.** `agent` and `agent-machine` send `X-AI-Proxy-Version` and `X-AI-Proxy-Protocol` with every request. A protocol outside the server's `MIN_PROTOCOL..PROTOCOL` gets `426 Upgrade Required` with how to update (WebSockets close with `4426`). Requests without the header, such as browsers', aren't checked.

## Agent-facing APIs

Agents call these paths on their loopback agent-proxy, which authenticates all upstream traffic with `X-AI-Proxy-Token` while preserving vendor Authorization. Session paths require an open session and control permission. Unscoped authenticated traffic can create a private implicit session.

| Method | Path | What it does |
|---|---|---|
| GET, POST | `/v1/{path}`, `/session/{session_id}/v1/{path}` | OpenAI: chat completions, responses, embeddings and the rest. `GET .../models` lists the backend's models. On an Ollama backend, chat completions are translated to native `/api/chat` with the model's profile applied; Strata, Docker Model Runner and llama.cpp get them unchanged. |
| POST | `/anthropic/v1/messages`, `/session/{session_id}/anthropic/v1/messages` | Anthropic Messages: translated for Ollama, passed through to Anthropic, Strata, Docker Model Runner and llama.cpp. |
| POST | `/anthropic/v1/messages/count_tokens`, `/session/{session_id}/anthropic/v1/messages/count_tokens` | Token counting: passed through where the backend has it, estimated for Ollama. |
| GET, POST, PUT, PATCH, DELETE | `/anthropic/{path}`, `/session/{session_id}/anthropic/{path}` | Any other Anthropic path, forwarded only with a hosted `anthropic` backend. |
| GET, POST, DELETE | `/api/{path}`, `/session/{session_id}/api/{path}` | Native Ollama, for what agents call only: `chat`, `generate`, `embed`, `embeddings`, `tags`, `show`, `ps`, `version`. Profiles apply to `chat` and `generate` on Ollama; Docker Model Runner's Ollama API gets them unchanged; Strata and llama.cpp have none. Anything else (`pull`, `delete`, ...) answers `404`: models are managed through their machine. The `agent-proxy` keeps its own unscoped `/api/...` for the launcher's calls, so agents use the session path. |
| GET, POST, DELETE | `/session/{session_id}/mcp/{machine_id}/{name}` | A shared MCP server, as Streamable HTTP, relayed to its machine through its tunnel. Requires the `key` query parameter in the URL returned only to the launcher; the public session record contains no such token. Only the servers the session was started with; whoever started it must still hold the machine's `mcp` grant. Each POST is recorded in the session with its reply. See [MCP](mcp.md). |

Account agents don't use these. Their HTTPS goes to the `agent-proxy`'s HTTPS proxy (`HTTPS_PROXY=http://<session>:agent-proxy@127.0.0.1:8183`), which carries each connection to `WS /api/proxy/tunnel` (below). The server refuses destinations that resolve to loopback, private, link-local or reserved IP addresses.

The catch-alls here are registered after every console route, so `/api/{path}` never shadows one.

If a nonstreaming upstream reply is interrupted after its headers arrive, the proxy
returns `502` and records the failure. The connection and in-flight entry are released,
so the session can be cleared or deleted. Cancelling a buffered request also releases
its in-flight entry and records `client_closed`.

## Health and the server

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/health` | Public | `status`, the running instance's id, the server's `version`, `protocol` and `min_protocol`. |
| GET | `/api/status` | Signed in | Totals, requests in flight, recent requests, intercept state, `default_backend`, whether a restart is needed for the listen address, and each machine backend's `provider`. Only sessions you can see (their token totals too), and only the backends your organization's agents may use. |
| GET | `/api/config` | Site admin | The editable network settings (`proxy.listen_host`, `listen_port`, `url`, `public_url`, `default_backend`, `backends`) and `restart_required`. |
| PUT | `/api/config` | Site admin | Stores them. Can remove local backends and add or edit hosted ones; refuses new or moved local ones. Without `backends`, leaves them alone; without `proxy.public_url`, leaves it as it is (`null` clears it). See [Configuration](configuration.md). |
| GET | `/api/config/export` | Site admin | Every setting as one YAML document (`proxy`, `default_backend`, `intercept`, `retention_days`, `backends`, `models`, `agents`); `?format=json` for JSON. |
| POST | `/api/config/import` | Site admin | Stores a settings document (YAML or JSON body): the parts it has, or with `?replace=true` every part. Everything is checked first. Answers `changed` and `restart_required`. |
| GET | `/api/config/agents` | Site admin | Every stored agent: `id`, `definition`, `yaml` (the definition as YAML, to edit), `builtin` (it ships with the server) and `edited` (changed since). |
| PUT | `/api/config/agents/{agent_id}` | Site admin | Adds or changes an agent; the body is its definition, YAML or JSON. A shipped one stops following the server's updates. `400` with the reason when it isn't valid. |
| DELETE | `/api/config/agents/{agent_id}` | Site admin | Removes an agent (`204`). A shipped one stays removed through updates, until it's added again or reset. |
| POST | `/api/config/agents/{agent_id}/reset` | Site admin | A shipped agent back to this version's definition, following updates again. `404` for others. |
| GET | `/api/config/models` | Site admin | Ollama model tuning: `defaults`, `profiles`, `aliases`. `?format=yaml` for YAML. |
| PUT | `/api/config/models` | Site admin | Stores the model tuning document (YAML or JSON body). |
| GET | `/api/config/storage` | Site admin | `database` (`kind`: `sqlite` or `postgresql`, `url` with its password hidden, `size` in bytes) and `retention_days`. |
| PUT | `/api/config/storage` | Site admin | `{"retention_days": days}` (1 to 36500), or `null` to keep recordings until deleted. Old recordings go at once. |
| GET | `/api/server/stats` | Site admin | The server computer's load and disk use, read when asked (nothing is saved): `hostname`, `platform`, `boot_time`; `cpu` (`percent`, `count`, `load_average`, null on Windows); `memory` and `swap` (bytes, `percent`); `process` (agent-server's `pid`, `rss`, `cpu_percent` of one core, `threads`, `started_at`); `disks`, each filesystem holding the log or build folder once (`mountpoint`, `total`, `used`, `free`, `percent`, `folders`); `folders` (`name`, `path`, `bytes`) and `database` (`kind`, `bytes`), measured in the background every five minutes, so `bytes` is null until `folders_measured_at` is set. |
| GET | `/api/server/access-log` | Site admin | Every HTTP request and WebSocket received since the server started, newest first (from memory). `?failed=true` keeps failures and dropped WebSockets only; `q` matches words in the path, address, user, user agent, status and reason; `limit` (1 to 1000, default 200); `before` (an `id`) pages back. Each entry: `id`, `time`, `kind` (`http`, `websocket`), `method` (`WS` for WebSockets), `path`, `query` (parameter names only), `client`, `forwarded_for`, `host`, `user_agent`, `program_version`, `protocol`, `user`, `tenant`, `status` (101 for an accepted WebSocket), `close_code`, `close_reason`, `closed_by` (`server`, `client`), `detail` (the start of an error's body), `machine_id` and `machine_name` (a fleet connection's, once its hello named it), `duration_ms`, `outcome` (`ok`, `failed`, `dropped`, `open`). Also `started_at`, `totals` (`ok`, `failed`, `dropped`, `open_websockets`) and `kept` (`all`, `failed`). |
| GET | `/api/server/access-log/stats` | Site admin | Since the server started: `totals`; `per_minute` (the last 60 minutes: `minute`, `ok`, `failed`, `dropped`); `answers` (`answer`, `count`: a status, or how a WebSocket ended, like `4401 by server` or `refused 403, 4401`); `durations` (`count`, `p50`, `p95`, `max` in ms, of the newest 2,000 HTTP answers); `failing_routes` (`method`, `route` with ids as `{id}`, `answer`, `count`, and the last one's `time`, `user`, `user_agent`, `program_version`, `reason`); `failing_clients` (`address`, `count`, the same last fields); `open_websockets` (`path`, `user`, `machine_id`, `machine_name`, `address`, `since`, `user_agent`, `program_version`). And `saved`, from the database: `days` (the last 14: `day`, `ok`, `failed`, `dropped`), `fleet` (each machine's connections over `fleet_days`, 7: `machine_id`, `machine_name`, `user`, `address`, `connections`, `ok`, `failed`, `dropped`, `connected_ms`, `last_time`, `last_answer`, `last_reason`, `user_agent`, `program_version`), `oldest`, `count`, `kept_days`. |
| GET | `/api/server/access-log/history` | Site admin | What's saved, across restarts, newest first: failures, dropped WebSockets and every fleet connection (entries as above, with `saved: true` and a string `id`). `failed=true`, `fleet=true` (fleet connections only), `q`, `limit` (1 to 1000), `before` (a `time`) pages back. Also `kept_days`. |
| POST | `/api/server/restart` | Site admin | Restarts with the stored settings once in-flight requests finish (up to 30 s). Answers `202` with `waiting_for` and the address it comes back on. `409` when the server can't restart itself (`--reload`). |

## Sessions and requests

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/api/sessions` | Signed in | Opens a session for other tools: `client`, `project`, `model`, `backend` (or `direct`), `trace`, `tags`, and sharing: `visibility`, `team`, `org`, `access`. A `shell` client is always private. |
| GET | `/api/sessions` | Signed in | The sessions you can see, each with its `machine` and what `you` may do. |
| DELETE | `/api/sessions` | Signed in | Deletes all saved traffic. Ended sessions and open ones idle for 15 minutes are deleted; recently active ones keep running but lose their data; ones with requests in flight are skipped. Only your own (and your organization's admins: its sessions without an owner). |
| GET | `/api/sessions/{session_id}` | Signed in, can view | One session. |
| PATCH | `/api/sessions/{session_id}` | Signed in, can control | Ends it (`ended`, `exit_status`), switches capture (`trace`), or for its owner changes its sharing (`visibility`, `team`, `org`, `access`). Narrowing the sharing drops terminal viewers who lose access. A shell can't be shared. |
| POST | `/api/sessions/{session_id}/stop` | Signed in, can control | Stops the session's replies the server is still reading for a client that dropped (Ask a model's **Stop**): `{"stopped": <count>}`. Ending the session does it too. |
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

Every machine route needs **Trusted**. A machine you may not see answers `404`. The server asks the machine over its fleet socket for everything about a model server; see [Machines](machines.md) and [Providers](providers.md). A model server is named `{kind}-{port}`, such as `ollama-11434`, `llamacpp-9931`, `strata-18500` or `docker-12434`.

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
| POST | `/api/machines/{machine_id}/providers/adopt` | Admin | Adds a model server the machine runs as a backend, for its organization's agents only: `{"type": "ollama"}`. A matching legacy backend without a machine (the root's) is linked in place, only for a machine in the root, retaining its name and settings. `409` if it's already one of this machine's backends. Another machine's backend at the same address doesn't count: machines behind one public address (a PC and its WSL, a home router) look alike to the server, and agents reach each one through its own machine. |
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

People's own AI plans and where they run; see [Subscriptions](subscriptions.md). All need **Signed in**; changes also **Same origin**. Answers are metadata only: none carries a token, except the one-time handover. Another person's subscription answers `404` unless you may see it.

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

A person who has reached a limit gets `429` with `error.code: "usage_limit"` and `Retry-After` from the agent APIs (`/v1`, `/anthropic`, `/api` model calls, and account agents' calls to the subscription's hosts), and `429` from `POST /api/agents/{agent_id}/sessions` and `POST /api/machines/{id}/launch` for that provider.

## Containers

Containers the fleet's machines run as backends. See [Machines](machines.md). Changes also need **Same origin**.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/containers` | Trusted | The machines you can see that could run containers (their CLI, whether it answers, the engines they offer), and the state of the containers on them. Another organization's containers answer `404` everywhere. |
| GET | `/api/containers/{name}` | Trusted | One container's state. |
| POST | `/api/containers` | Admin, machine owner | Creates one on a connected machine with `agent-machine containers on`: `name`, `machine`, `engine`, and `image`, `port`, `gpus`, `env`, `volume`. Saves it as a backend. |
| PUT | `/api/containers/{name}` | Admin, machine owner | Changes its settings; they apply when it next starts. The engine can't change. |
| DELETE | `/api/containers/{name}` | Admin, machine owner | Removes it from its machine and from the server's backends. |
| POST | `/api/containers/{name}/{action}` | Trusted | `start`, `stop` or `restart`. Answers `202` with the job. `409` with `conflicts` when other containers hold the GPU it needs; send `{"switch": true}` to stop them first. |
| GET | `/api/containers/{name}/log` | Trusted | Its container log, `?tail=` lines (10 to 5000, default 400). |

## Terminals and pairing

Shared terminals are a remote shell, so these routes need **Trusted** and **Same origin**. See [Accounts](accounts.md).

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/remote` | Signed in | `{trusted: true, local: false, paired: false, accounts: true}`, for launchers checking they may share a terminal. No access key is returned. |
| POST | `/api/remote/code`, `/api/remote/rotate` | Signed in | Always 403: the access key and pairing codes were removed. |
| POST | `/api/remote/pair` | Signed in | Always 410: pairing was removed; sign in through OIDC. |
| POST | `/api/remote/unpair` | Signed in, Same origin | Removes only the legacy pairing cookie; use `/api/auth/logout` to sign out. |
| GET | `/api/terminals` | Trusted | The shared terminals you can see: agent, machine, project, model, backend, state, activity, requests in flight and `attention` (waiting, with the question on screen). |

The two terminal WebSockets are below.

## Accounts, organizations and users

See [Accounts](accounts.md). Changes answer with the directory (`orgs` and `users`) as you may see it. Authentication is always required.

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/auth/status` | Public | Always `required: true`; current identity, HTTPS, issuer, audience, CLI client id and `redirect_uris` (the browser sign-in's registered callbacks; the Android app keeps sign-in in its WebView on their hosts). |
| GET | `/api/auth/login` | Public | Starts PKCE browser authorization; `return_to` must match an allowed console origin. |
| GET | `/api/auth/callback` | Public | Validates OIDC state, code exchange and nonce; adds the account to the browser's sign-ins and makes it active. A sixth account goes back with `?sign_in=too_many`. |
| POST | `/api/auth/refresh` | Public, Same origin | Rotates the active account's refresh token and renews the access cookie. |
| GET | `/api/auth/accounts` | Public | The accounts this browser is signed in to: `active` (its name, or null), `accounts` (`user`, `active`), and `max` (how many one browser holds, 5). Names only; tokens never leave the cookie. |
| POST | `/api/auth/switch` | Public, Same origin | `{"user"}`: makes another of this browser's accounts the active one, with a fresh access cookie. No password. `404` for an account it isn't signed in to; `401` when that sign-in has ended (it's dropped). |
| POST | `/api/auth/logout` | Public, Same origin | Signs out of the active account, or `{"user": name}`, or every one with `{"all": true}`, revoking each at the issuer. When the active one goes, the next one left becomes active. Answers the accounts left. |
| POST | `/api/auth/device` | Signed in (console), Same origin | Approve an `agent login`/`agent-machine login` code as yourself, without a password: `{user_code, action}`. `check` answers what asked (`client`, `requested_at`, `expires_at`, `user`) and decides nothing; `approve` or `deny` answers `{approved}`. `404` wrong or expired code, `409` already decided, `403` not a console sign-in, `429` too many tries. |
| POST | `/api/auth/login` | Public | 410; password sign-in moved to the issuer. |
| POST | `/api/auth/token`, `/api/auth/password` | Signed in | 410; legacy password grants and account password changes removed. |
| GET, POST, DELETE | `/api/auth/tokens`, `/api/auth/tokens/{token_id}` | Signed in | 410; launcher keys replaced by OAuth grants. |
| GET | `/api/orgs` | Signed in | Organizations with their teams and members (the users whose organization it is), each with `root` and `parent` (`"root"`, or `null` for the root itself). You see your own; site admins see every one, the root first. |
| POST | `/api/orgs` | Site admin | `{"id", "name"}`: adds an organization (a tenant, under the root). |
| PUT | `/api/orgs/{org}` | Admin | `{"name"}`: renames it. |
| DELETE | `/api/orgs/{org}` | Site admin | Removes an organization with no users left (`400` otherwise). The root can't be removed. |
| PUT / DELETE | `/api/orgs/{org}/members/{name}` | Admin | Users can't join or leave organizations: `400`, except a `PUT` for the user's own organization, which changes nothing. |
| POST | `/api/orgs/{org}/teams` | Admin | `{"id", "name"}`: adds a team. |
| PUT | `/api/orgs/{org}/teams/{team}` | Admin | `{"name"}`: changes the display name, keeping its id and sharing key. |
| DELETE | `/api/orgs/{org}/teams/{team}` | Admin | Removes a team. |
| PUT / DELETE | `/api/orgs/{org}/teams/{team}/members/{name}` | Admin | Adds/removes one team membership, for a user of that organization. No body needed. |
| GET | `/api/users` | Signed in | Users with their `role`, `tenant` (their organization; `orgs` is `[tenant]`) and `teams`. You see the people in your organization; site admins see everyone. |
| POST | `/api/users` | Admin | `{"name", "password", "role", "tenant", "teams"}`: adds a user to one organization, for good. An organization's admin adds to their own (`tenant` may be left out); a site admin names any, and without one makes a site admin (`role: "admin"` only). `orgs: [one]` still works for `tenant`. |
| PUT | `/api/users/{name}` | Admin | Changes `role`, `teams` or `password` together, only if all changes are valid. `orgs` may only repeat the user's own organization. An organization's last admin can't be made a member; root users stay admins. |
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
| `/api/machines/connect` | `agent-machine run`, with its token | One machine in the fleet, for as long as it runs. In: its `hello` (id, name, installed agents, model servers, containers and engines, launch and server settings, shared MCP servers), then `update` every minute, `ping`, `token` (its new access token before the old one expires, so the server keeps the socket open; ignored unless valid and the same user's), and answers (`launched`, `launch_failed`, `container_result`, `server_result`, `provider_result`, `provider_progress`, `folders_result`, `update_result`). Out: `welcome`, then requests: `launch`, `shell`, `tunnel`, and `container`, `server`, `provider`, `folders` and `update` requests with an `op`. A removed machine is closed with `4403`. |
| `/api/machines/tunnel/{tunnel_id}` | `agent-machine`, with its token, when the server asks | One connection of agents' traffic (or MCP traffic) to a port on that machine: raw bytes both ways. Closed with `4404` for an unknown tunnel. |
| `/api/proxy/tunnel` | `agent-proxy`, with its machine's token | One account agent's HTTPS connection, which the server decrypts and records with its CA. Binary frames both ways. Closed with `4404` when capture is off. |
| `/api/terminals/{session_id}/host` | The launcher (`--control`), relayed by its `agent-proxy` | The launcher sends a hello, then the agent's output as binary frames and `resize`, `gap` and `exit` messages. The server sends `resume` (where to resend from) and `input` (`parts` to type, sent a short gap apart). Only someone who can control the session may host it. |
| `/api/terminals/{session_id}/view` | The console's terminal page | `?from=<offset>` (optional): the byte offset of the output the viewer already has. The server sends `state`, `{"type": "replay", "start": <offset>, "resumed": <bool>}` (resumed: what follows continues from `from`, so nothing is redrawn), the history as binary frames, `replayed`, `gap` (`{"type": "gap", "to": <offset>}`: output the launcher skipped), then live output and `state`, `resize` and `error` messages. The viewer sends `{"type": "input", "text": "...", "enter": true}` or `{"type": "input", "keys": "\u001b"}`; typing needs control of the session, checked on every message. A viewer that falls behind is closed with `4408`, and a missing terminal with `4404`. |

## Check

This page lists all 125 routes the app registers, from `app.routes` of a server built in a temporary folder: 120 HTTP routes, the 5 WebSockets above, plus the `/assets` static mount.

| Area | Routes |
|---|---|
| Agent-facing | 10 (5 paths, each unscoped and under `/session/{session_id}/`) plus the MCP relay |
| Health and the server | 16 |
| Sessions and requests | 13 |
| Live traffic | 2 |
| Agents and models | 4 (with `/api/mcp`) |
| HTTPS capture | 3 |
| Machines and providers | 18, plus 2 WebSockets |
| Containers | 7 |
| Terminals and pairing | 6, plus 2 WebSockets |
| Accounts | 27 |
| Downloads | 6 |
| Console | 3 (with the documentation's files) |
| OpenAPI | 4 |
| Proxy tunnel | 1 WebSocket |

That is 10 + 1 + 16 + 13 + 2 + 4 + 3 + 20 + 7 + 8 + 27 + 6 + 3 + 4 + 1 = 125. Each is in a table above.
