# Server (`server/agent_server/`)

`agent-server`: Python, FastAPI and uvicorn. The hub. It owns the API, accounts, all configuration (`config/`), agent definitions and template rendering, backend choice, recording, HTTPS interception of account agents, and the fleet registry. It optionally serves the console's build; `AI_PROXY_SERVE_UI=0` disables UI routes and automatic UI builds for separate hosting. The same package holds `agent-auth` (`issuer.py`), the OIDC issuer the API depends on but runs as its own process, and `agent-benchmark` (`benchmark.py`).

## Design in one paragraph

Every agent request arrives at one ASGI app. `routes/compat.py` finds the session, picks its backend, then either translates the request (Ollama) or forwards it (Strata, Docker, Anthropic). Each request is tracked by `Runtime` and recorded under `logs/sessions/<id>/`. Machines hold a WebSocket open to the server (`/api/machines/connect`); the server asks them to do everything about a model server (`Fleet.ask`), and reaches the model servers on machines behind NAT through tunnels that the machine dials back. Account agents' HTTPS arrives as WebSocket tunnels and is decrypted with the server's own CA. Shared state lives in one `Runtime`, and there are no globals apart from two in `strata.py`: `managed_keys` (filled by `KeyStore`) and `fleet` (set by `Runtime`, for the keys machines report).

## Must never

- Call a model server for anything but agents' traffic. Listing, pulling, status, logs, metrics, Ollama settings and start/stop are fleet requests to the machine. Native `/api/*` passthrough only allows `compat.NATIVE_AGENT_PATHS`.
- Run `docker`, or add a local backend (`MACHINE_TYPES`: ollama, strata, docker) by URL. `Settings.update_network_config` refuses new or moved ones; only `anthropic` is added by URL.
- Import client, machine or Python-port code, or assume anything about them beyond the API.
- Keep cookies in the upstream client (`runtime.upstream_client`), replace an agent's own `Authorization` on hosted pass-through, or forward it to an MCP bridge.
- Send platform credentials upstream: `compat._upstream_headers` drops `X-AI-Proxy-Token`, cookies, Origin and the session/backend headers, and drops `Authorization` only when it was the platform's Bearer (no `X-AI-Proxy-Token`).
- Treat anything but the middleware's introspected identity as authentication: not loopback, a session id or the old access key.

## Composition

```mermaid
flowchart LR
  main["app.main()"] --> sup["Supervisor.serve loop<br/>Settings.load · ui_build · ensure_first_user"]
  sup --> create["create_app(settings)"]
  create --> rt["Runtime<br/>sessions · chats · terminals · fleet · tunnels<br/>remote · auth · containers · live · client · intercept_client"]
  create --> mw["SessionMiddleware → CORS → VersionMiddleware → AuthMiddleware"]
  create --> routers["auth · management · terminals · proxy ·<br/>containers · machines · mcp · <b>compat</b> ·<br/>downloads · help-files · UI fallback"]
```

- **Router order is load-bearing.** `compat` has catch-alls (`/api/{path}`, `/v1/{path}`, `/anthropic/{path}`), so every console route registers before it, and the UI fallback comes last. Within a router, literal paths come before parameterised ones.
- **Middleware order** puts the version check before auth, so old programs get 426 (WebSocket close 4426) before a 401. `routes/auth.register` adds Starlette's `SessionMiddleware` (the signed `mesh_oidc_state` cookie for the browser sign-in) after the others, so it is the outermost.
- **Split console/API**: `Settings.console_origins` (`AI_PROXY_CONSOLE_ORIGINS`) is the exact origin allowlist for credentialed CORS and `remote.same_origin`, shared by auth mutations and route/WebSocket gates. Cookies stay host-only on the API; no parent-domain cookie or wildcard origin.
- **Restart** (`POST /api/server/restart` → `Runtime.request_restart`): waits up to 30 s for in-flight requests (`Runtime.active`; container jobs run on machines and aren't waited for), then `_restart_when_idle` closes live streams, terminals, fleet sockets, tunnels and intercept tunnels, and the Supervisor serves again with a fresh `Settings.load()`. **A new long-lived connection must be closed there.** There's no Supervisor with `--reload` or in tests.
- `ensure_first_user` runs in `main()` (with `--reload` too) and every `Supervisor.serve` round, including loopback, never in `create_app`. It does nothing once `users.json` exists or `AI_PROXY_USERNAME`/`AI_PROXY_PASSWORD` are set. Tests must authenticate explicitly; no anonymous local mode remains.
- `issuer.py` runs the separate `agent-auth` Authlib service (`create_auth_app`, never mounted into the API app): PKCE, device and service grants, hashed transactional SQLite token state (`State`), discovery/JWKS, introspection, revocation and userinfo. It reads but never mutates API accounts. API `Auth` introspects with a dedicated client (closed in the lifespan); browser `routes/auth.py` manages host-only access/refresh cookies. See `auth.md`.

## Module map

| Area | Modules | Key symbols |
| --- | --- | --- |
| Config | `config.py` | `Settings` (`load`, `backend`, `backend_for`, `profile`, `update_network_config`, `save_backend`, `remove_backend`), `Backend` (`name`, `url`, `type`, `machine`, `container`), `serves()`, `BACKEND_TYPES`, `NATIVE_PROTOCOLS`, `MACHINE_TYPES`, `PROVIDER_NAMES` |
| Shared state | `runtime.py` | `Runtime` (`track` → `attach` → `stream_object`/`progress` → `complete`, `proxy_error`, `resolve_session`, `agents()`), `upstream_client` |
| Recording | `sessions.py`, `telemetry.py`, `live.py`, `chat.py` | `SessionStore`, `RequestTelemetry` (`kind`: api, intercept, upgrade, tunnel, mcp), `LiveHub` (memory only), `Chats` (`chat-index.json`) |
| Agents | `agents.py` | `AgentDefinition.render`, `load_agents` (re-read on every call), `_render` (a template with a missing placeholder is dropped) |
| Translation | `translate/` | `openai_to_ollama`/`OpenAIStreamTranslator`, `anthropic_to_ollama`/`AnthropicStreamTranslator`, `apply_profile` |
| Providers | `strata.py`, `model_runner.py` | `key_for`/`with_key` (container key, `STRATA_API_KEY`, or the machine's), `upstream_path` |
| Fleet | `fleet.py`, `tunnels.py`, `containers.py`, `mcp.py` | `Fleet` (`connect`, `message`, `ask`, `launch`, `shell`, `remove`/`restore`, `moved`, `machine_for`), `allowed`, `FEATURES`, `ADMIN_FEATURES`; `TunnelHub.base_url`; `ContainerManager`, `validate_spec`, `KeyStore`; `mcp.usable`/`choose` |
| Security | `auth.py`, `access.py`, `remote.py` | `AuthMiddleware`, `PROTECTED_PREFIXES`, `PUBLIC_PATHS`, `ADMIN_ONLY`, `Auth` (`identify` by introspection, `identity_for`, `account_version`, `check_password`), `AuthUnavailable`, `UserStore`, `Identity`, `request_identity`; `can_view`/`can_control`/`is_owner`, `client_tags`, `sharing_tags`, `SERVER_TAGS`; `RemoteAccess.trusted`, `same_origin`, `write_secret`/`read_token` |
| Issuer (`agent-auth`) | `issuer.py` | `create_auth_app`, `Issuer` (`client`, `provision`, `active`, `rate`, `csrf`), `State` (SQLite `records`, signing key), grants `CodeGrant`/`RefreshGrant`/`DeviceGrant`/`ServiceGrant`, `main` (`agent-auth`, `agent-auth client add|remove`) |
| I/O | `intercept.py`, `certs.py`, `websocket_tap.py`, `terminals.py` | `InterceptProxy.serve_tunnel`, `CertificateAuthority`, `WebSocketTap`, `TerminalHub` |
| Distribution | `version.py`, `downloads.py`, `installer.py`, `openapi.py`, `ui_build.py` | `PROTOCOL`, `MIN_PROTOCOL`, `VersionMiddleware`; `Downloads` (only `SHA256SUMS` files); `SAFE_URL`; `TAGS` |
| Routes | `routes/` | `compat` (agents), `management` (console), `machines` (fleet and provider relay), `terminals`, `mcp`, `proxy`, `containers`, `auth` (browser sign-in, organizations, users) |
| Tools | `benchmark.py` | `agent-benchmark`, runs `benchmarks/tests/<name>/` |

`routes/auth.py` exposes organization → team → member management, including scoped PUT/DELETE membership routes and team display-name changes. `UserStore` owns the hierarchy and applies scoped changes under its lock; combined account edits validate role, memberships and password before saving once. The existing user records and `<org>/<team>` sharing keys stay compatible.

## Core flows

**Agent request** (`routes/compat.py`)

```mermaid
sequenceDiagram
  participant A as agent-proxy
  participant C as compat
  participant R as Runtime
  participant T as TunnelHub
  participant U as Upstream
  A->>C: /session/<id>/v1/... (or X-AI-Proxy-Session)
  C->>C: session → backend (X-AI-Proxy-Backend or session.backend)
  alt ollama and translatable
    C->>C: openai_to_ollama / anthropic_to_ollama + apply_profile
  else strata / docker / anthropic
    C->>C: _forward: headers cleaned, strata key, docker path
  end
  C->>R: track / attach
  C->>T: base_url(backend)
  T-->>C: loopback listener if tunnelled, else backend.url
  C->>U: runtime.client stream
  U-->>C: chunks → stream_object, live deltas
  C->>R: complete → requests.jsonl, trace
```

- Before compat runs, `AuthMiddleware` has already required an access token and, for a session path or `X-AI-Proxy-Session`, an open session the caller can control (404 otherwise).
- `MESSAGES_BACKENDS` (anthropic, strata, docker) take Anthropic Messages as they are; Ollama gets them translated.
- **Every upstream URL comes from `await runtime.tunnels.base_url(backend)`.** It tunnels when the machine reports `tunnel`, is connected and isn't loopback: a loopback listener per `(machine, port)`. Each connection sends `{"type":"tunnel"}` on the fleet socket and waits for the machine to dial `WS /api/machines/tunnel/<id>`.

**Account interception**: authenticated `WS /api/proxy/tunnel` → `InterceptProxy.serve_tunnel` → a socketpair into `_accept` (`start_tls` needs a real transport). `_connection` needs an identity (the tunnel's, or on the optional direct listener an `X-AI-Proxy-Token` in the CONNECT head, introspected; 503 if the issuer is down) and `Proxy-Authorization` naming an open session that identity can control, or answers 407. A destination that resolves to a non-public address gets 403. `intercept.passthrough` hosts pipe untouched (`kind: tunnel`); others are decrypted and recorded with credential redaction (`redact_headers`, `redact_fields`).

Account interception's outbound HTTP goes through its own client (`runtime.intercept_client`, `InterceptTransport` with `PublicNetworkBackend`), which resolves and pins destinations to public IP addresses only. The passthrough tunnel and WebSocket upgrade paths apply the same check (`_public_addresses`) before connecting; private, loopback, link-local and reserved addresses are refused.

**Fleet socket** (`/api/machines/connect`): signed with the machine's token (4401 otherwise); the first text frame is the hello → `Fleet.connect` (4400 bad id, 4403 removed or someone else's id, 4409 replaces an older socket) → `claim` backends → `welcome`. Then:

| Direction | Messages |
| --- | --- |
| machine → server | `update`, `ping`, `launched`/`launch_failed`, `provider_progress`, `<channel>_result` (accepted only from the machine that was asked) |
| server → machine | `launch`, `shell`, `tunnel`, and `Fleet.ask` channels: `container` (status, start, stop, restart, remove, logs), `provider` (status, pull, delete, load, unload, log, metrics, search), `server` (settings, configure, start, stop, restart), `folders`, `update` |

`Fleet.ask` errors: not connected or the machine's toggle for that channel is off → 409, reply `ok:false` → 409, timeout → 504, disconnect → 502. `Machine.keys` (Strata keys) and `Machine.mcp_key` stay in memory only, never in `stored()` or `public()`.

**MCP relay** (`/session/<id>/mcp/<machine>/<name>`): session open and given that server (`tags["mcp"]`), the launch-only per-session URL token matches its stored hash, the launcher (`launched_by`, else `owner`) still `mcp.usable`, machine connected with a bridge, name still listed. The request goes to `127.0.0.1:<bridge>` through `tunnels.base_url` with the bridge key and only `REQUEST_HEADERS`. POSTs are recorded (`kind:"mcp"`). The middleware has already required an access token and `can_control` on the session; the token travels as `?key=` (`mcp.valid_token` against `Session.mcp_token_hash`, issued by `mcp.issue_token` when the launch chose servers).

**Terminals**: the launcher's `.../{id}/host` (`can_control`) sends a hello → `TerminalHub.attach`, then binary output. Viewers on `.../view` (`can_view`) get state, the history in 256 KB chunks, `replayed`, then live output. Input is re-checked with `can_control` on every message and goes out as `{"type":"input","parts":[...]}` (`input_parts`, with bracketed paste). Slow viewers are closed with 4408.

## State

- `Fleet` receives the session store from `Runtime`. Launch replies must come from the
  requested machine; before granting `launched_by`, it checks the returned session's
  owner, machine, launch id and client. Machine history is filtered by session access.
- Legacy backends (no `machine`) are linked only by explicit admin adoption
  (`providers/adopt`). `claim` only moves already-linked backends to the machine's new
  address (`Fleet.moved`); nothing links a backend to a machine automatically.
- Buffered compat replies use `_read_upstream` to close the response and complete
  telemetry on read failure or cancellation. `_cancel_request` also handles a
  cancellation while awaiting upstream headers.
- Terminal viewers re-identify their original credential for input and each output
  frame, and poll permissions every second while idle. A missing identity on an
  originally authenticated connection never becomes local-mode access.

| In memory | On disk |
| --- | --- |
| `Runtime.active`/`recent`, `LiveHub`, fleet sockets and pending asks, `Machine.keys`/`mcp_key`, tunnel listeners, terminal viewers; in the issuer process, its sign-in rate counters (`Issuer.attempts`) | `logs/sessions/<id>/` (`session.json`, with only the MCP token's hash, `requests.jsonl`, `traces/NNNNNN.json`, `terminal.log`, `terminal.json`), `logs/chat-index.json`, `logs/machines.json`, `logs/proxy/proxy.log` |
| | Account state, in the folder of `Settings.remote_key_path` (`certs/` for a real server, `AI_PROXY_REMOTE_KEY_FILE` moves it; `log_dir/remote/` in code-built Settings): `users.json` (scrypt hashes, orgs and teams; obsolete sign-ins/tokens do not authenticate), `oidc-browser-key`, `container-keys.json`, `first-user-password`. The access key file itself is no longer created. Issuer state (`AI_PROXY_OIDC_STATE_DIR`, else `oidc/` in the same folder): `issuer.sqlite3` and `signing-key.pem`, owner-only. |
| | `certs/`: `ca.pem`, `ca-key.pem`, `bundle.pem`. `config/backends.yaml` is rewritten with `yaml.safe_dump` from the console, so its comments are lost |

## Security model

The summary; `auth.md` has the whole of it (credentials, the middleware's order, roles, what must never change).

- **Mandatory OIDC**: `AuthMiddleware` covers `/api/`, `/v1/`, `/anthropic/`, `/session/` and API documentation, except explicit public authentication endpoints. Active access tokens are introspected at the separate issuer (`Auth.identify`; down or unconfigured is 503); identity and permissions come from current accounts. Loopback, session ids and legacy keys never authenticate. Retired auth routes (password sign-in, launcher tokens, pairing) answer 410 or 403.
- **Browser and role checks**: unsafe methods require the API's exact origin or an explicitly allowed console origin, and `ADMIN_ONLY` routes need an admin. WebSockets periodically revalidate credentials and close with retryable 4410 when renewal is required.
- **Sessions** (`access.py`): owner or `launched_by`; private/team/org visibility; view or control. A session you can't see is a 404. Clients can't set `SERVER_TAGS`. Shell sessions are forced private. Narrowing sharing drops terminal viewers.
- **Machines** (`fleet.allowed`): the owner always; admins only `ADMIN_FEATURES` (view, models, update); others through org or team grants in `Machine.access`. Launches and folders are checked against roots on both sides. Creating, editing or removing a container needs the owner, who must also be an admin (ADMIN_ONLY).

## Extending

- **Route**: add it to a `routes/*.py` `register` (or a new module registered before `compat`), an `openapi.TAGS` entry, an `ADMIN_ONLY` entry if it changes the server, the sharing check (`viewable`/`controllable`) or the feature check (`allowed`), and a line in `docs/api.md`.
- **Backend type**: `config` constants, compat (`MESSAGES_BACKENDS`, `_forward`, `_list_models`), `management._backend_models`, plus the machine and console sides. See the checklist in `AGENTS.md`.
- **Fleet message**: a `Fleet.ask` channel with its gate and `<channel>_result` in `Fleet.message`, a hello field in `_update`/`Machine`, then the machine side in Go and Python.
- **Agent**: an entry in `config/agents.yaml`; no code.

## Tests (`server/tests/`)

- `make_app(tmp_path, handler)`/`close_app`: `Settings(...)` built in code, `create_app`, a `TestClient` (signed in: `oidc_helpers.authenticated_client`, with introspection mocked), and `runtime.client` swapped for an `httpx.MockTransport`. Assert on what upstream received too.
- `oidc_helpers.py` plays the issuer's introspection for API tests (`access_token(app, user)`, `issuer(app).revoke`); `test_oidc_issuer.py`, `test_oidc_api.py` and `test_oidc_browser.py` cover the real issuer, the API's mandatory auth and the browser sign-in; `test_quickstart.py` the quickstart stack's credentials and layout. See `auth.md`.
- `FakeSocket` (`test_providers.py`) answers like agent-machine; `FakeMachine` (`test_containers.py`) replaces `Fleet.ask`; `ws_connect` (`test_terminals.py`) makes WebSockets look local.
- `test_client.py` runs the real Go client and machine (and, from `python/tests`, the Python ones) on real ports and real tunnels, signed in through a real `create_auth_app` issuer, with `fake_mcp.py`, `fake_docker.py`, `fake_ollama.py`, `fake_strata.py` (`fake_systemctl.py` is for the machine's systemd tests in Go and `python/tests`).
- `conftest.py` fails any test that touches the real `config/*.yaml` or creates secrets in `certs/` (`remote-access-key`, `container-keys.json`, `users.json`, `first-user-password`). Pass `backends_file=tmp_path / "backends.yaml"` when saving config.

## Known gaps

- `containers._answers` probes `backend.url + health_path` directly, not through the tunnel, so readiness can be wrong for a machine behind NAT.
- `conftest.py` doesn't guard `certs/oidc-browser-key`, which `routes/auth.register` creates beside `users.json`; code-built Settings keep it under `log_dir/remote/`.
