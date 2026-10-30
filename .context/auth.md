# Authentication and authorization

Read this before changing credentials, permissions or routes. Authentication is mandatory, including loopback and an empty account store. `Auth.enabled` is always true. Historical sharing helpers still accept `Identity | None` for internal compatibility: **no protected HTTP or WebSocket handler may receive an anonymous identity**.

## Boundaries

- `issuer.py` runs **agent-auth**, a separate Authlib OAuth/OIDC issuer at `https://auth.aicontrolmesh.com`. It reads existing accounts through `UserStore` but never writes `users.json`; credentials, grant transactions and signing material belong to its own state directory.
- `auth.py` runs the API resource boundary. A dedicated, proxy-free httpx client introspects opaque access tokens using the confidential API client's secret. Network work runs in a thread, not on the ASGI loop. Failure is 503, never anonymous fallback.
- `routes/auth.py` is the browser BFF: Authlib discovery, authorization code + S256 PKCE, state/nonce and ID-token validation, access/refresh cookies, renewal and revocation. Passwords are submitted only to the issuer.
- `access.py`, `fleet.py` and route helpers retain existing roles, session sharing and per-machine feature grants. The issuer does not replace authorization or assign privileges from untrusted claims.
- `remote.py` trusts only the middleware identity. Loopback and the legacy access key confer no API access. Pairing is obsolete.

## Credentials

| Credential | Transport | Persistence and validation |
| --- | --- | --- |
| Account password | Issuer's CSRF-bound sign-in forms | Existing scrypt hash in `users.json`. Issuer uses `Auth.check_password` and rate limits. |
| Access token | `Authorization: Bearer`, `X-AI-Proxy-Token`, or API cookie `ai_proxy_session` | Opaque, five-minute token; issuer stores only its hash. API validates active, issuer, audience, `mesh-api` scope, Bearer type, expiry and current account identity. An ID token is not accepted. |
| Refresh token | Token endpoint; browser cookie `mesh_refresh`, path `/api/auth` | Hash at issuer, rotating, absolute 30-day family lifetime; replay revokes the family. CLI stores local credential secrets with restricted permissions and cross-process refresh serialization. |
| ID token | Code exchange only | RS256, discovery/JWKS, client audience and nonce; verified by Authlib, never an API credential. |
| Service-client secret | HTTP Basic to `/token` | Explicitly provisioned client bound to an existing least-privilege user. Hash at issuer. |
| API introspection secret | HTTP Basic to `/introspect` | Shared secret through environment/secret manager, never config YAML. This resource client cannot mint service-account tokens. |
| Session id / MCP capability | Agent paths and MCP launch URL | Session id selects the session, not an authentication bypass. MCP additionally requires its launch-only hashed capability and current machine grant. |

Issuer SQLite transactions cover grant validation, consumption and token persistence. Signing key and database are private files. `Auth.account_version` binds tokens to the password hash and account creation time; reset, deletion and same-name recreation invalidate credentials. Current role, organizations and teams are reloaded at the API boundary.

## Request pipeline

1. `VersionMiddleware` rejects unsupported protocols (426 / WebSocket 4426). Protocol 2 removes legacy authentication.
2. `AuthMiddleware` checks Origin on unsafe methods and WebSockets before calling any handler, including machine tunnel callbacks. Exact API origin or `console_origins` only; no parent-domain cookies. Native clients may omit Origin.
3. Explicit public paths may run unsigned: `/health`, `/api/auth/status`, `/api/auth/login`, `/api/auth/callback`, `/api/auth/refresh`, `/api/auth/logout`. Static console/docs/downloads/installers remain public outside protected prefixes.
4. Every other protected path requires an introspected access token. Initial failures are 401 / 4401; unavailable issuer is 503.
5. Session agent paths additionally require an open session and `access.can_control`; admin routes check `ADMIN_ONLY`.
6. Handlers check session visibility/control or machine feature grants. Hidden sessions and machines answer 404.

WebSockets revalidate credentials every 15 seconds and close 4410 for renewal/revocation or issuer failure. Terminal routes also check credentials and memberships before input/output and when idle. Live SSE checks each event and heartbeat. Reconnecting clients must renew before dialing.

## Browser and native clients

- Browser authorization callbacks and return origins use exact allowlists. Signed `mesh_oidc_state` is short-lived; access and refresh cookies are HttpOnly, host-only, SameSite=Lax and Secure for HTTPS deployments. The console never stores OAuth tokens in localStorage. Auth status refresh is fail-closed; refreshes are serialized within a tab and with Web Locks across tabs.
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
- Machine owner always has its features; admins implicitly have view/models/update only. Launch, shell and MCP require their specific grants and machine toggles. Roots are checked server-side and again after resolving paths on the machine.
- The API owns account/directory mutations; the issuer is read-only. Adding or removing all users never disables authentication.
- Bootstrap runs on first API process start, including loopback; `create_app` never manufactures a test identity.

## Extending and testing

Add admin policy, session checks or machine-feature checks for each new route, document it in `docs/api.md`, and cover signed-out, wrong-user and role/grant failures. Public paths must be deliberately added, never inferred from an open session.

`test_oidc_issuer.py` exercises grants, PKCE, device approval, replay/revocation, service clients and hashed state. `test_oidc_api.py` covers mandatory auth and credential isolation. `test_oidc_browser.py` runs the real issuer through the API BFF. `quickstart/tests/test_e2e.py` signs in against the running Compose stack. Existing contract tests exercise Go and Python against a real server/issuer. Permission suites use controlled introspection without weakening production middleware.

Operational configuration, endpoints and migration are in `docs/oidc.md`. Issuer outage, missing secrets, expired credentials and malformed metadata must surface as errors, never local-mode success.
