# Authentication and authorization

Read this before changing credentials, permissions or routes. Authentication is mandatory, including loopback and an empty account store; there is no no-accounts mode and no code for one. The permission helpers (`access.can_view`/`can_control`/`is_owner`, `fleet.owns`/`allowed`/`can_manage`/`removed_for`, `mcp.usable`) still accept `Identity | None`, but `None` is denied everything: **no protected HTTP or WebSocket handler may receive an anonymous identity**, and none does (the middleware sets one; `routes/mcp.py` and `intercept.py` check for `None` before asking).

## Boundaries

- `issuer.py` runs **agent-auth** (`create_auth_app`, console script `agent-auth`, `127.0.0.1:10000` unless `AI_PROXY_AUTH_HOST`/`AI_PROXY_AUTH_PORT`), a separate Authlib OAuth/OIDC issuer at `https://auth.aicontrolmesh.com` (`AI_PROXY_OIDC_ISSUER`). It is never mounted into the API app. It reads existing accounts through `UserStore` but never writes `users.json`; credentials, grant transactions and signing material belong to its own state directory (`AI_PROXY_OIDC_STATE_DIR`, else `oidc/` beside `users.json`: `issuer.sqlite3`, `signing-key.pem`). It refuses to start without `AI_PROXY_OIDC_INTROSPECTION_SECRET`.
- Built-in clients (`Issuer.client`): the console (`mesh-console`: authorization code + refresh, only the registered redirect URIs), the CLI (`mesh-cli`: device code + refresh) and the API's introspection client (`mesh-api`: no grants). Service clients (`client_credentials`) are rows in the issuer's SQLite, added with `agent-auth client add <id> --user <name>` and removed with `agent-auth client remove <id>`.
- `auth.py` runs the API resource boundary. `Auth.identify` uses a dedicated, proxy-free httpx client to introspect opaque access tokens at `<issuer>/introspect` with the confidential API client's secret. The middleware runs it in a thread, not on the ASGI loop. Failure or a missing secret raises `AuthUnavailable` (503), never anonymous fallback.
- `routes/auth.py` is the browser BFF: Authlib discovery, authorization code + S256 PKCE, state/nonce and ID-token validation, access/refresh cookies, renewal and revocation. Passwords are submitted only to the issuer. The old password, launcher-token and password-change routes (`POST /api/auth/login`, `/api/auth/token`, `/api/auth/tokens*`, `/api/auth/password`) answer 410.
- `access.py`, `fleet.py` and route helpers retain existing roles, session sharing and per-machine feature grants. The issuer does not replace authorization or assign privileges from untrusted claims.
- `remote.py` trusts only the middleware identity (`RemoteAccess.trusted`). Loopback and the legacy access key confer no API access, and the key file is no longer created. Pairing is gone: `GET /api/remote` only reports status (`trusted`, with `local`/`paired` always false and `accounts` always true), `POST /api/remote/code` and `/rotate` answer 403 for everyone, `/pair` 410, and `/unpair` only clears the old cookie (`remote.COOKIE`). These tombstones stay so old consoles and scripts get a clear refusal.

## Credentials

| Credential | Transport | Persistence and validation |
| --- | --- | --- |
| Account password | Issuer's CSRF-bound sign-in forms | Existing scrypt hash in `users.json`. Issuer uses `Auth.check_password` and its own in-memory rate limits (`Issuer.rate`). `AI_PROXY_USERNAME`/`AI_PROXY_PASSWORD` define one more account, an admin, kept outside `users.json` (`Auth.environment_user`). |
| Access token | `Authorization: Bearer`, `X-AI-Proxy-Token`, or API cookie `ai_proxy_session` | Opaque, five-minute token; issuer stores only its hash. API validates active, issuer, audience, `mesh-api` scope, Bearer type, expiry and current account identity. An ID token is not accepted. |
| Refresh token | Token endpoint; browser cookie `mesh_refresh`, path `/api/auth` | Hash at issuer, rotating, absolute 30-day family lifetime; replay revokes the family. CLI stores local credential secrets with restricted permissions and cross-process refresh serialization. |
| ID token | Code exchange only | RS256, discovery/JWKS, client audience and nonce; verified by Authlib, never an API credential. |
| Service-client secret | HTTP Basic to `/token` | Explicitly provisioned client bound to an existing member account, never an admin (`Issuer.provision`). Hash at issuer. Its tokens stop working once that account changes password, is recreated or becomes an admin (`Issuer.active`). |
| API introspection secret | HTTP Basic to `/introspect` | Shared secret through environment/secret manager, never config YAML. This resource client cannot mint service-account tokens. |
| Session id / MCP capability | Agent paths and MCP launch URL | Session id selects the session, not an authentication bypass. MCP additionally requires its launch-only hashed capability and current machine grant. |

Issuer SQLite transactions cover grant validation, consumption and token persistence. Signing key and database are private files. `Auth.account_version` binds tokens to the password hash and account creation time (the environment account's to its password); reset, deletion and same-name recreation invalidate credentials. Current role, organizations and teams are reloaded at the API boundary (`Auth.identity_for`). `users.json` may still hold old versions' browser sign-ins and launcher tokens (`sessions`, `tokens`); no code reads them (removing a user still drops theirs), and nothing authenticates with them. Tests plant them with `oidc_helpers.plant_legacy_credentials`.

## Request pipeline

1. `VersionMiddleware` rejects unsupported protocols (426 / WebSocket 4426). Protocol 2 (`MIN_PROTOCOL` = `PROTOCOL` = 2) removes legacy authentication.
2. Only `PROTECTED_PREFIXES` (`/api/`, `/v1/`, `/anthropic/`, `/session/`, `/docs`, `/redoc`, `/openapi.json`) go through `AuthMiddleware`; the console, `/help-files/`, `/downloads/` and the installers stay public outside them.
3. On those, it checks Origin (`remote.same_origin`) on unsafe methods and every WebSocket before anything else, public paths and machine tunnel callbacks included. Exact API origin or `console_origins` only; no parent-domain cookies. Native clients may omit Origin.
4. `PUBLIC_PATHS` may run unsigned: `/health`, `/api/auth/status`, `/api/auth/login`, `/api/auth/callback`, `/api/auth/refresh`, `/api/auth/logout`. `/api/auth/status` still introspects a credential it is sent, and answers 503 when it can't.
5. Every other protected path requires an introspected access token: `X-AI-Proxy-Token`, else `Authorization: Bearer`, else the `ai_proxy_session` cookie. No token is 401 / 4401; unavailable issuer is 503 (on a WebSocket `_deny` closes with 4400 + status % 100, so 4403).
6. HTTP requests matching `ADMIN_ONLY` need an admin (403). A `/session/<id>/...` path, or `/v1/` and `/anthropic/` with `X-AI-Proxy-Session`, additionally needs an open session the identity `access.can_control`s, else 404. The identity is put in `scope["state"]` (`auth.request_identity`).
7. Handlers check session visibility/control or machine feature grants. Hidden sessions and machines answer 404.

WebSockets revalidate credentials every 15 seconds (`CREDENTIAL_CHECK_INTERVAL`) and close 4410 for renewal/revocation, a different user or issuer failure. Terminal routes also check credentials and memberships before input/output and when idle. Live SSE checks each event and heartbeat. Reconnecting clients must renew before dialing.

## Browser and native clients

- Browser authorization callbacks and return origins use exact allowlists (`oidc_redirect_uris`, `oidc_browser_origins`; `Settings.__post_init__` refuses wildcards and non-HTTPS outside loopback). Signed `mesh_oidc_state` (Starlette `SessionMiddleware`, keyed by `oidc-browser-key` beside `users.json`) lasts ten minutes; access and refresh cookies are HttpOnly, host-only, SameSite=Lax and Secure for HTTPS deployments. The console never stores OAuth tokens in localStorage. Auth status refresh is fail-closed; refreshes are serialized within a tab and with Web Locks across tabs.
- `agent login` and `agent-machine login` use discovery and device authorization; each owns its credentials. Refresh happens before authenticated requests and WebSocket reconnects. Logout revokes credentials and reports failures.
- The issuer's forms need `Origin` equal to the issuer. Keep its `Referrer-Policy` at `same-origin` (under `no-referrer` browsers post `Origin: null`) and the redirect URIs' origins in its CSP `form-action` (browsers apply it to the redirect back to the callback). TestClient sets `Origin` by hand and can't catch either; `quickstart/tests` runs them against a real stack.
- Device approval and denial both require the issuer's CSRF-bound sign-in form. Either decision consumes the browser transaction and fixes the code's decision; denied codes return `access_denied` and cannot be approved later.
- The agent-proxy signs **all** API-bound traffic with `X-AI-Proxy-Token`, including model/MCP requests; it strips caller-supplied platform headers and browser cookies/Origin. Its loopback Host/Origin checks remain essential because it signs as its user.
- Preserve the vendor `Authorization` when an alternate platform header authenticated the request. Strip platform credentials before upstream calls; never send vendor authorization to an MCP bridge.
- Python mirrors both Go programs, including credential format, grants, refresh locking and protocol.
- Android discovers the issuer only from the chosen API, without HTTP redirects, and allows only that HTTPS issuer and API auth paths inside its WebView. External sites remain external.

## Authorization invariants

- Private sessions remain private from admins; owners and `launched_by` control their sessions. Shared viewers control only with explicit control access.
- Clients cannot set server-owned tags. Narrowing sharing disconnects viewers. Shell sessions stay private.
- Machine owner always has its features; admins implicitly have view/models/update only (`ADMIN_FEATURES`). Launch, terminal (shells) and MCP require their specific grants and machine toggles. Roots are checked server-side and again after resolving paths on the machine.
- The API owns account/directory mutations; the issuer is read-only. Adding or removing all users never disables authentication.
- Bootstrap (`app.ensure_first_user`) runs on API process start, including loopback, unless `users.json` exists or the environment account is set: it adds `admin` and saves its password in `first-user-password`. `create_app` never manufactures a test identity.

## Extending and testing

Add admin policy, session checks or machine-feature checks for each new route, document it in `docs/api.md`, and cover signed-out, wrong-user and role/grant failures. Public paths must be deliberately added, never inferred from an open session.

`test_oidc_issuer.py` exercises grants, PKCE, device approval, replay/revocation, service clients and hashed state. `test_oidc_api.py` covers mandatory auth and credential isolation. `test_oidc_browser.py` runs the real issuer through the API BFF. `test_quickstart.py` checks the quickstart's credentials against a real issuer and its Compose layout; `quickstart/tests/test_e2e.py` signs in against the running Compose stack. `test_client.py` runs Go (and, from `python/tests`, Python) against a real server and `create_auth_app` issuer. Permission suites use controlled introspection without weakening production middleware: `tests/oidc_helpers.py` swaps `Auth.client` for a mock introspection endpoint (`Issuer.issue`/`revoke`, `access_token`) and `authenticated_client` is a `TestClient` signed in as `test-admin`.

Operational configuration, endpoints and migration are in `docs/oidc.md`. Issuer outage, missing secrets, expired credentials and malformed metadata must surface as errors, never local-mode success.
