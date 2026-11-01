# The audit log

The audit log records who did what, to what, and whether it worked: every change to the server or an organization, every console sign-in and sign-out, every refusal, and every look at someone else's recordings. It never holds what was said to a model, and never a secret. Hash chains and signed checkpoints support integrity checks; audit retention is separate from recording retention, and exports can send events to your collector. These mechanisms can support an organization’s controls but do not establish certification, compliance or an independent security review. The signing key and primary records live in the same database: retain independently protected checkpoints and exports for stronger evidence.

## What's recorded

| Category | Events |
| --- | --- |
| `authentication` | Console sign-in and sign-out; refused requests to sign-in routes |
| `accounts` | Users, organizations, teams and memberships made, changed or removed; passwords changed (that they were, never the password) |
| `configuration` | Server settings, agents, Ollama model tuning, backends, the audit log's own settings, restarts |
| `data` | Retention policies and legal holds; looks at another person's traces, chats or request records (`data.viewed`) |
| `machines` | Machines connecting, disconnecting and refused; removed and restored; who may use them; launches, terminals and remote updates; model and server operations |
| `execution` | Containers made, changed, started, stopped and removed |
| `subscriptions` | Provider accounts made, assigned and revoked; launch grants; Copilot tokens handed to a launch |
| `usage` | Usage limits set and removed |
| `sessions` | Sessions started (agents launched), their sharing changed, ended, deleted, traced |
| `interception` | Account agents' HTTPS refused (no session it may control, or a private destination) |
| `*.refused` | Any change refused (401, 403), and any admin route refused |

Each event is a [CloudEvent](https://cloudevents.io) with an OCSF-like body:

```json
{"specversion": "1.0", "id": "6f6c…", "source": "agent-server/api-7d9…", "type": "mesh.audit.data.update",
 "time": "2026-11-01T12:00:00.000Z", "tenant": "acme", "actor": "dave", "outcome": "success",
 "data": {"tenant": "acme", "actor": {"user": "dave", "via": "cookie", "ip": "10.1.2.3", "user_agent": "…"},
          "action": "PUT /api/orgs/{id}/retention", "target": {"kind": "orgs", "id": "acme", "part": "retention"},
          "outcome": "success", "reason": null, "status": 200, "request_id": null, "session_id": null, "machine_id": null,
          "changes": {"fields": ["days"], "days": {"traces": 7}}},
 "chain": {"seq": 1042, "prev": "9b1…", "hash": "e47…"}}
```

`changes` lists the names of the fields a request sent, and the values only of fields that are settings or names (visibility, roles, days, limits, ...), never a password, token or key. `outcome` is `success`, `refused` (401, 403, 404, 407, 409, 429) or `failure`.

A change is written to the log before it's answered, so a change someone saw succeed is always in it. Refusals and looks at recordings are written straight after.

## Integrity

- Each organization's events are a **hash chain**: every event has a `seq` and the hash of the one before it (`prev`), and its own `hash` covers both. Removing, reordering or changing one breaks the chain from there.
- The database refuses to change audit events, and lets only retention delete them: triggers check this on every row, whatever role writes.
- Every hour (Server settings › Audit, `checkpoint_minutes`) the worker **signs a checkpoint**: every chain's head, signed with the server's audit key (Ed25519, kept in the database's secrets). With blob storage, checkpoints are also written to `audit/checkpoints/`; with S3 Object Lock as the bucket's default, they can't be changed or deleted even by an administrator of the server.
- `agent-server audit verify` (or **Verify** on the Audit log page, `GET /api/audit/verify`) recomputes every chain and checks it against the checkpoints and their signatures. It exits 1 and lists each problem when anything doesn't match. `agent-server audit key` prints the public key, to check checkpoints somewhere else; `agent-server audit checkpoint` signs one now.

## Reading it

- **Audit log** in the console (Server): admins see their organization's events, site admins everyone's. Filter by type, person and outcome, open an event for its whole record, download CSV or NDJSON.
- `GET /api/audit`, `/api/audit/export?format=csv|ndjson`; see [API](api.md#retention-and-the-audit-log).

## Exports

Server settings › Audit (site admins), or `PUT /api/config/audit`, lists where events go. Each export has a cursor, so one that was down catches up from where it stopped; `agent_server_audit_backlog` says how far behind the slowest is.

| Kind | `url` | Sends |
| --- | --- | --- |
| `otlp` | `https://collector:4318` | OTLP/HTTP JSON logs to `<url>/v1/logs`: one record an event, with `event.name`, `event.id`, `mesh.tenant`, `user.name`, `event.outcome`, `source.address` and `mesh.action` as attributes and the whole event as its body. Through an OpenTelemetry Collector to Splunk, Elastic, Microsoft Sentinel, Datadog and the like. |
| `syslog` | `syslog+tls://siem:6514` (or `syslog://` over plain TCP) | RFC 5424 messages over TCP (RFC 5425 TLS), each the event as JSON. |
| `webhook` | `https://hooks.example/audit` | `POST {"events": [...]}`; with `token_env`, signed: `X-Mesh-Signature: sha256=<HMAC of the body>`. |
| `blob` | | NDJSON files in blob storage under `prefix` (default `audit/export/`). |

`token_env` names an environment variable on the server holding the export's secret (an OTLP collector's token, sent as `Authorization`, or the webhook's signing key), never the value itself. `tenants` limits an export to some organizations.

```json
{"checkpoint_minutes": 60,
 "exports": [
   {"name": "siem", "kind": "otlp", "url": "https://otel-collector.observability:4318", "token_env": "AUDIT_OTLP_TOKEN"},
   {"name": "archive", "kind": "blob", "prefix": "audit/export"}
 ]}
```

## How long it's kept

`days.audit` in the [retention policy](retention.md), at least 90 days whatever anyone sets (365 by default). A legal hold on an organization keeps its events. Exports keep their own copies for as long as your SIEM does.

## agent-auth

The issuer's own sign-in pages (passwords and device codes typed there) log to agent-auth's standard output; collect it with the rest of the cluster's logs. Console sign-ins, sign-outs and device approvals through the console are in the audit log.
