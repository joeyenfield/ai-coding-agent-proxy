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

Then run `docker compose --profile console up -d --build`, with the introspection secret supplied in the environment. Compose adds `agent-auth` on **host loopback port 10000**, with its own persistent state volume. The issuer reads the existing `users.json` read-only; the API still owns account, organization and team administration. Both services must see the same accounts file. Do not run multiple independent writable copies.

The Windows HTTPS setup includes `auth.<HostName>` in its certificate and sends that hostname to `-AuthBackend` (default `http://127.0.0.1:10000`). `-AuthHostName` overrides the hostname. Provision DNS and TLS for the new host before using it. Auth responses must not be cached, and auth URLs must not enter access logs.

Without containers, run `agent-auth` and `agent-server` as separate processes under the same service account. `agent-auth` listens on `127.0.0.1:10000` by default. Configure `AI_PROXY_AUTH_HOST` and `AI_PROXY_AUTH_PORT` when needed. Trust forwarded headers only from your reverse proxy; do not expose the issuer's plain HTTP listener publicly.

On first API start, including loopback, the server bootstraps an admin if `users.json` does not exist. Reset its password with `agent-server user passwd admin` and remove `first-user-password`. Adding, changing or removing accounts never disables authentication.

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

Callback URLs and console return origins must be explicitly registered. No wildcards or arbitrary return URLs are allowed. The issuer requires PKCE S256. The API verifies the ID token and nonce through Authlib.

Access tokens stay in an HttpOnly API cookie; refresh tokens stay in a separate HttpOnly cookie restricted to `/api/auth`. They are not stored in localStorage. The console renews access automatically and serializes refreshes between tabs using Web Locks where available. Keep the console and API on HTTPS subdomains of the same site so host-only `SameSite=Lax` cookies work. Sign-out revokes the refresh family before removing the cookies.

The Android WebView keeps the explicitly discovered HTTPS issuer and API sign-in callback in the app, so the resulting cookie belongs to the WebView, not an external browser. Other external links still open in the browser.

## CLI sign-in

```bash
agent login https://api.aicontrolmesh.com
agent-machine login https://api.aicontrolmesh.com
```

The command prints the issuer's verification address and a short user code. Open it in your browser, sign in, check the code and approve **only a sign-in you started**. To reject a code, sign in on that page and choose **Deny device sign-in**. Denial is final for that code; the CLI receives `access_denied` and no tokens are issued. Start a new CLI sign-in if needed. The command polls the token endpoint until approved, denied or expired. No password is read by the CLI or sent to the API.

Each program keeps its own credentials file, including its access token, rotating refresh token and expiry. Refreshes are serialized across processes before atomically saving new credentials. `agent logout` and `agent-machine logout` revoke their credentials. Expired or revoked credentials fail closed.

The agent-proxy signs every server-bound call, including model and MCP traffic, with an access token in `X-AI-Proxy-Token`. That alternate header preserves the coding agent's own vendor `Authorization`. The server strips platform credentials before forwarding to providers. Existing session and MCP permissions still apply.

## Automation

Provision a confidential client at the issuer, associated with a dedicated, least-privilege existing account:

```bash
agent-auth client add build-bot --user automation
```

Keep the generated client secret in your secret manager. Request an access token using HTTP Basic client authentication and a form:

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
| GET | `/userinfo` | OIDC identity associated with an access token. |
| POST | `/introspect` | Active access-token metadata, requiring the API's confidential client. |
| POST | `/revoke` | Revoke a client's token or refresh family. |
| GET | `/health` | Health check. |

On the **API host**:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/auth/status` | Public authentication requirement, current identity, issuer, audience and CLI client id; no credentials disclosed. |
| GET | `/api/auth/login` | Start browser sign-in. Optional `return_to` must match an allowed console origin. |
| GET | `/api/auth/callback` | Verify state, PKCE exchange and OIDC identity; set cookies. |
| POST | `/api/auth/refresh` | Renew the browser's access token using its refresh cookie. |
| POST | `/api/auth/logout` | Revoke the browser's refresh family and clear cookies. |

Mutations and WebSocket connections with an `Origin` header require the API's origin or an exact allowed console origin. Native clients may omit that header. Foreign origins are rejected before a protected handler runs. Old password-login and launcher-token creation endpoints no longer issue credentials.

## Settings and persistence

| Variable | Default / meaning |
| --- | --- |
| `AI_PROXY_OIDC_ISSUER` | `https://auth.aicontrolmesh.com`; HTTPS required, except explicit loopback development issuers. |
| `AI_PROXY_OIDC_AUDIENCE` | `https://api.aicontrolmesh.com`. |
| `AI_PROXY_OIDC_CONSOLE_CLIENT` | `mesh-console`; public code/refresh client. |
| `AI_PROXY_OIDC_CLI_CLIENT` | `mesh-cli`; public device/refresh client. |
| `AI_PROXY_OIDC_INTROSPECTION_CLIENT` | `mesh-api`; confidential resource client. |
| `AI_PROXY_OIDC_INTROSPECTION_SECRET` | Required shared confidential-client secret; never put it in `config/`. |
| `AI_PROXY_OIDC_REDIRECT_URIS` | Comma-separated exact API callback URLs. |
| `AI_PROXY_OIDC_BROWSER_ORIGINS` | Comma-separated exact console return origins. |
| `AI_PROXY_OIDC_STATE_DIR` | Issuer database and private signing key; defaults to `oidc/` beside `users.json`. |
| `AI_PROXY_AUTH_HOST`, `AI_PROXY_AUTH_PORT` | Issuer listen address (`127.0.0.1:10000`). |

Back up the issuer's persistent state and signing key securely. Authorization, device and refresh state is transactional; secrets are hashed at rest. Access tokens expire quickly. Refresh tokens rotate and have an absolute family lifetime; replay revokes the family. Password reset and account removal invalidate issued credentials. Roles and memberships are resolved from the current account on API requests.

## Migration from protocol 1

Upgrade the server, Go/Python clients and machines together. Protocol 2 removes password grants, unauthenticated local mode and access-key pairing. Existing `aip_` launcher tokens, browser sign-in cookies and pairing links no longer grant access. Sign in again through OIDC. Session ids and vendor API keys are not replacements for access tokens. Missing issuer configuration or an unavailable issuer never restores anonymous access.
