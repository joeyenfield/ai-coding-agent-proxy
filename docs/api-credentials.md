# Using API credentials

An API credential lets a script, a CI job or another service call the server's API without a person signing in. It's an OAuth client: a client ID and a secret, which you exchange at the sign-in service (the issuer) for a short-lived access token. Every call then sends that token.

A credential always acts as one account, with that account's role, organization and teams. It never gets more rights than the account has. Tenant, sharing and machine checks apply exactly as they do to the person.

API credentials are not API provider keys (the keys the server uses to reach OpenAI or DeepSeek), and not machine setup codes.

## Create one

1. Create a dedicated account for the job, with only the role and teams it needs. A member account is enough for most automation.
2. In the console, open **Organizations**, pick the organization and open the **API credentials** tab. You need to be an administrator of that organization.
3. Give the credential a description that says what uses it, choose the account it acts as (**Act as account**) and click **Create credential**.
4. Copy the client ID and the secret. **The secret is shown once.** Put it straight into your secret manager or CI secret store.

An organization's administrators can only choose accounts in their own organization. Site administrators can also create **bootstrap** credentials in Site administration, for setting a new server up (see [OIDC authentication](oidc.md#automation)).

You can also create one on the issuer host with `agent-auth client add <client-id> --user <account>`. Add `--management` for an administrator account.

## Get an access token

Find the token endpoint in the API credentials tab, or in the issuer's `/.well-known/openid-configuration` (`token_endpoint`). It's the issuer's address followed by `/token`, for example `https://auth.example.com/token`.

Send the client ID and secret with HTTP Basic authentication, and ask for the `client_credentials` grant with scope `mesh-api`:

```bash
curl -s https://auth.example.com/token \
  -u "$CLIENT_ID:$CLIENT_SECRET" \
  -d grant_type=client_credentials \
  -d scope=mesh-api
```

The answer is JSON:

```json
{"access_token": "...", "token_type": "Bearer", "expires_in": 1800, "scope": "mesh-api"}
```

The token lasts 30 minutes. There is no refresh token: when it expires, ask for a new one the same way.

## Call the API

Send the token in the `Authorization` header:

```bash
TOKEN=$(curl -s https://auth.example.com/token -u "$CLIENT_ID:$CLIENT_SECRET" \
  -d grant_type=client_credentials -d scope=mesh-api | jq -r .access_token)

curl -s https://mesh.example.com/api/sessions -H "Authorization: Bearer $TOKEN"
```

The same in Python, getting a new token shortly before the old one expires:

```python
import time

import httpx

ISSUER = "https://auth.example.com"
SERVER = "https://mesh.example.com"


class MeshClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.credentials = (client_id, client_secret)
        self.token, self.expires = "", 0.0
        self.http = httpx.Client(base_url=SERVER)

    def _token(self) -> str:
        if time.time() > self.expires - 60:
            answer = httpx.post(f"{ISSUER}/token", auth=self.credentials,
                                data={"grant_type": "client_credentials", "scope": "mesh-api"})
            answer.raise_for_status()
            body = answer.json()
            self.token, self.expires = body["access_token"], time.time() + body["expires_in"]
        return self.token

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.http.get(path, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs)


mesh = MeshClient("build-bot", "the-secret-from-your-secret-store")
print(mesh.get("/api/sessions").json())
```

A `401` means the token expired or the credential stopped working (see below). A `403` means the account isn't allowed to do that. [API](api.md) lists every endpoint and who may call it.

## Try it in the API reference

The server's OpenAPI document is at `/openapi.json`, with Swagger UI at `/docs` and ReDoc at `/redoc` (the console links them from **Server settings › API reference**). They need a sign-in too.

In Swagger UI, click **Authorize**, and under **apiCredential** enter the client ID and secret and tick `mesh-api`. Swagger UI fetches a token from the issuer and sends it with every **Try it out**. The calls run as the credential's account, so use one with as few rights as you can.

To generate a client, download the document with a token and feed it to your OpenAPI generator:

```bash
curl -s https://mesh.example.com/openapi.json -H "Authorization: Bearer $TOKEN" -o agent-server.openapi.json
```

## Keep it safe

- **One credential per job,** each acting as its own dedicated account. Then you can revoke one without stopping the others, and the audit log shows which job did what.
- **Regenerate secret** gives a new secret and ends every token made with the old one. Do it when someone who knew the secret leaves, or on a schedule.
- **Revoke** disables the credential and its tokens for good. Revoke bootstrap credentials as soon as setup is done: the console shows a warning to site administrators until you do.
- A credential also stops working when its account's password is reset, the account is deleted, or the account's role changes. Create a new one afterwards.
- A credential can't create, regenerate or revoke credentials. Only an administrator signed in to the console can.
- Keep the secret out of source code, logs and command lines that others can see. Read it from an environment variable or a secret store.

A revoke or a regenerate ends the credential's existing tokens at once, not when they expire.
