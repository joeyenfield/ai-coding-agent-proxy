# OIDC authentication

Authentication is required on every API interface, including loopback. The platform runs its own Authlib-based OIDC issuer, **`agent-auth`**, separately from `agent-server`:

| Host | Responsibility |
| --- | --- |
| `auth.aicontrolmesh.com` | Sign-in, authorization, device approval, token issuance, refresh, revocation and discovery. |
| `api.aicontrolmesh.com` | The resource API. Validates access tokens with authenticated introspection; enforces roles, sharing and machine grants. |
| `console.aicontrolmesh.com` | The console. Redirects sign-in through the API to the issuer; never collects your password. |

OIDC identifies people; OAuth **access tokens** authorize API requests. An ID token is not an API credential. Session ids identify agent sessions but no longer authenticate requests.

## Deployment

Install the server's dependencies, which include Authlib:

```powershell
python -m pip install -e "server[dev]"
```

Set `AI_PROXY_OIDC_INTROSPECTION_SECRET` to a strong random secret on both services through your deployment's secret manager. Do not commit it. The API authenticates as `mesh-api` when introspecting tokens; that client cannot issue automation tokens.

For the public deployment, configure:

```dotenv
VITE_API_URL=https://api.aicontrolmesh.com
AI_PROXY_SERVE_UI=0
AI_PROXY_CONSOLE_ORIGINS=https://console.aicontrolmesh.com
AI_PROXY_OIDC_ISSUER=https://auth.aicontrolmesh.com
AI_PROXY_OIDC_AUDIENCE=https://api.aicontrolmesh.com
AI_PROXY_OIDC_REDIRECT_URIS=https://api.aicontrolmesh.com/api/auth/callback
AI_PROXY_OIDC_BROWSER_ORIGINS=https://console.aicontrolmesh.com
```

Then run `docker compose --profile console up -d --build`, with the introspection secret supplied in the environment. Compose adds `agent-auth` on **host loopback port 10000**, keeping its state in the same PostgreSQL as the API, in its own schema (`mesh_auth`) and through its own role (`AI_PROXY_AUTH_DATABASE_URL`). The issuer reads the API's accounts read-only (that role can only `SELECT` them); the API still owns account, organization and team administration. Both services must use the same database. Do not run multiple independent copies of it.

The Windows HTTPS setup includes `auth.<HostName>` in its certificate and sends that hostname to `-AuthBackend` (default `http://127.0.0.1:10000`). `-AuthHostName` overrides the hostname. Provision DNS and TLS for the new host before using it. Auth responses must not be cached, and auth URLs must not enter access logs.

Without containers, run `agent-auth` and `agent-server` as separate processes under the same service account. With PostgreSQL, `agent-auth` uses `AI_PROXY_AUTH_DATABASE_URL`, or else the API's `AI_PROXY_DATABASE_URL` (its own schema). With SQLite, it opens the API's database file read-only for accounts and keeps its own state in `issuer-state.sqlite3` in its state folder. `agent-auth` listens on `127.0.0.1:10000` by default. Configure `AI_PROXY_AUTH_HOST` and `AI_PROXY_AUTH_PORT` when needed. Trust forwarded headers only from your reverse proxy; do not expose the issuer's plain HTTP listener publicly.

On first API start, including loopback, the server bootstraps an admin if accounts were never set up in its database. Reset its password with `agent-server user passwd admin` and remove `first-user-password`. Adding, changing or removing accounts never disables authentication.

## Browser sign-in

```mermaid
sequenceDiagram
  participant B as Browser
  participant A as API
  participant I as Auth issuer
  B->>A: GET /api/auth/login
  A-->>B: Redirect with state, nonce and S256 PKCE
  B->>I: Sign in and approve authorization
  I-->>B: One-time code to registered API callback
  B->>A: GET /api/auth/callback
  A->>I: Exchange code with PKCE verifier
  I-->>A: Access, refresh and verified ID tokens
  A-->>B: Host-only HttpOnly cookies; return to console
```

The issuer's sign-in page looks like the console. A wrong password shows the page again with the username kept. Each page works once and for 10 minutes, and each has its own cookie, so several open at once (other tabs) don't interfere. An expired or already-used page explains that and links back to the console to start again. If the API can't complete a sign-in, or the issuer isn't answering, the browser goes back to the console's sign-in page (`?sign_in=failed` or `?sign_in=unavailable`), which says what happened.

Callback URLs and console return origins must be explicitly registered. No wildcards or arbitrary return URLs are allowed. The issuer requires PKCE S256. The API verifies the ID token and nonce through Authlib.

Access tokens stay in an HttpOnly API cookie; refresh tokens stay in a separate HttpOnly cookie restricted to `/api/auth`. They are not stored in localStorage. The console renews access automatically and serializes refreshes between tabs using Web Locks where available. Keep the console and API on HTTPS subdomains of the same site so host-only `SameSite=Lax` cookies work. Sign-out revokes the refresh family before removing the cookies.

The Android WebView keeps the explicitly discovered HTTPS issuer and API sign-in callback in the app, so the resulting cookie belongs to the WebView, not an external browser. Other external links still open in the browser.

## CLI sign-in

```bash
agent login https://api.aicontrolmesh.com
agent-machine login https://api.aicontrolmesh.com
```

The command prints the issuer's verification address and a short user code. Open it in your browser, sign in, check the code and approve **only a sign-in you started**. To reject a code, sign in on that page and choose **Deny device sign-in**. Denial is final for that code; the CLI receives `access_denied` and no tokens are issued. Start a new CLI sign-in if needed. Already signed in to the console? Enter the code on **Machines › Approve a device** instead: the console shows what asked and when, and you approve or deny without your password. The API passes your console access token to the issuer (`POST /device/console`, authenticated as `mesh-api`), which accepts only console sign-ins, so a CLI or service token can't approve devices. The command polls the token endpoint until approved, denied or expired. No password is read by the CLI or sent to the API. [Security](security.md#device-approval) walks through the whole flow, what protects a code, and how to approve safely.

Each program keeps its own credentials file, including its access token, rotating refresh token and expiry. Refreshes are serialized across processes before atomically saving new credentials. `agent logout` and `agent-machine logout` revoke their credentials. Expired or revoked credentials fail closed.

The agent-proxy signs every server-bound call, including model and MCP traffic, with an access token in `X-AI-Proxy-Token`. That alternate header preserves the coding agent's own vendor `Authorization`. The server strips platform credentials before forwarding to providers. Existing session and MCP permissions still apply.

## Automation

Provision a confidential client at the issuer, associated with a dedicated, least-privilege existing account:

```bash
agent-auth client add build-bot --user automation
```

`agent-auth client remove build-bot` revokes it. Keep the generated client secret in your secret manager. Request an access token using HTTP Basic client authentication and a form:

```text
POST https://auth.aicontrolmesh.com/token
Authorization: Basic <client credentials>
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=mesh-api
```

Send the resulting token to the API as `Authorization: Bearer <access_token>`. `AI_PROXY_TOKEN` can supply a pre-issued access token to the launcher. Automation must obtain another token before expiry; a client-credentials grant does not turn a token into a permanent key. A service account receives its associated account's existing role and grants, not implicit admin rights.

## Endpoints

On the **auth host**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/.well-known/openid-configuration` | Issuer metadata. |
| GET | `/.well-known/jwks.json` | Public ID-token verification keys. Never private key material. |
| GET, POST | `/authorize` | Browser authorization code flow and sign-in/approval. |
| POST | `/token` | Code exchange, refresh, device grant or client credentials. No password or implicit grants. |
| POST | `/device_authorization` | Begin a CLI device authorization. |
| GET, POST | `/device` | Sign in and explicitly approve or deny a device code. |
| POST | `/device/console` | The API only (`mesh-api` client authentication), with a signed-in person's console access token: `action=check` describes a device code (`client_id`, `requested_at`, `expires_at`, `user`); `approve` or `deny` decides it as that person. `invalid_user_code` (400), `code_already_used` (409), `invalid_token` for anything but a console token (401), 20 a person per 10 minutes. |
| GET | `/userinfo` | OIDC identity associated with an access token. |
| POST | `/introspect` | Active access-token metadata, requiring the API's confidential client. |
| POST | `/revoke` | Revoke a client's token or refresh family. |
| GET | `/health` | Health check. |
| GET | `/sign-in.css` | The stylesheet of the issuer's own pages. |

On the **API host**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/auth/status` | Public authentication requirement, current identity, issuer, audience and CLI client id; no credentials disclosed. |
| GET | `/api/auth/login` | Start browser sign-in. Optional `return_to` must match an allowed console origin. |
| GET | `/api/auth/callback` | Verify state, PKCE exchange and OIDC identity; set cookies. |
| POST | `/api/auth/refresh` | Renew the browser's access token using its refresh cookie. |
| POST | `/api/auth/logout` | Revoke the browser's refresh family and clear cookies. |
| POST | `/api/auth/device` | Signed in to the console: check, approve or deny a CLI device code, relayed to the issuer's `/device/console`. |

Mutations and WebSocket connections with an `Origin` header require the API's origin or an exact allowed console origin. Native clients may omit that header. Foreign origins are rejected before a protected handler runs. Old password-login and launcher-token creation endpoints no longer issue credentials.

## Settings and persistence

| Variable | Default / meaning |
| --- | --- |
| `AI_PROXY_OIDC_ISSUER` | `https://auth.aicontrolmesh.com`; HTTPS required, except explicit loopback development issuers. |
| `AI_PROXY_OIDC_AUDIENCE` | `https://api.aicontrolmesh.com`. |
| `AI_PROXY_OIDC_CONSOLE_CLIENT` | `mesh-console`; public code/refresh client. |
| `AI_PROXY_OIDC_CLI_CLIENT` | `mesh-cli`; public device/refresh client. |
| `AI_PROXY_OIDC_INTROSPECTION_CLIENT` | `mesh-api`; confidential resource client. |
| `AI_PROXY_OIDC_INTROSPECTION_SECRET` | Required shared confidential-client secret; never put it in the server's settings or a settings export. |
| `AI_PROXY_OIDC_REDIRECT_URIS` | Comma-separated exact API callback URLs. |
| `AI_PROXY_OIDC_BROWSER_ORIGINS` | Comma-separated exact console return origins. |
| `AI_PROXY_AUTH_DATABASE_URL` | The issuer's database, such as `postgresql+psycopg://mesh_auth:<password>@host:5432/mesh`. Default: the API's PostgreSQL URL, else a SQLite file in the state folder. |
| `AI_PROXY_OIDC_STATE_DIR` | The issuer's state folder: its SQLite file without PostgreSQL, and where an older install's `issuer.sqlite3` and `signing-key.pem` are imported from. Defaults to `oidc/` in the account-state folder (`certs/` normally). |
| `AI_PROXY_AUTH_HOST`, `AI_PROXY_AUTH_PORT` | Issuer listen address (`127.0.0.1:10000`). |

Back up the issuer's state securely: its sign-ins, grants and signing key are in the database (`mesh_auth`), so a backup of the database covers them (see [Deployment](deployment.md)). An older install's `issuer.sqlite3` and `signing-key.pem` are imported on the issuer's first start against an empty database, so existing sign-ins keep working. Authorization, device and refresh state is transactional; secrets are hashed at rest. Access tokens last 5 minutes, authorization codes 2 minutes, and device codes and sign-in pages 10 minutes. Refresh tokens rotate and their family lasts at most 30 days; replay revokes the family. Password attempts are limited to 10 per address in 10 minutes. Password reset and account removal invalidate issued credentials. Roles and memberships are resolved from the current account on API requests.

## Migration from protocol 1

Upgrade the server, Go/Python clients and machines together. Protocol 2 removes password grants, unauthenticated local mode and access-key pairing. Existing `aip_` launcher tokens, browser sign-in cookies and pairing links no longer grant access. Sign in again through OIDC. Session ids and vendor API keys are not replacements for access tokens. Missing issuer configuration or an unavailable issuer never restores anonymous access.
