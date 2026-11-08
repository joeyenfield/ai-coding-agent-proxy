# Retention: what's recorded, and for how long

Implicit sessions created by unscoped agent writes use seven-day session retention when the organization's policy would keep sessions forever. A longer minimum retention still applies. Explicit sessions keep the configured policy. Read-only agent calls do not create implicit sessions. This applies to new sessions; existing recordings keep their stamped policy.

The server records every agent's model calls (who called which model, tokens, timings), and with tracing their full requests and replies, and shared terminals' output. How much of that is kept, and for how long, is a **retention policy**: the server's, set by site admins, and each organization's own, set by its admins within the server's bounds. A **legal hold** pauses scheduled retention deletion for what it covers. Manual session deletion and clearing do not currently check holds; this is an incomplete legal-preservation control.

## What a policy says

| Part | Values | Meaning |
| --- | --- | --- |
| `capture.traces` | `off`, `opt_in` (the default), `always` | Whether full requests and replies are recorded: never, per session (`--trace`, or the session's switch), or always. |
| `capture.intercept_bodies` | on (default) or off | Whether account agents' HTTPS traffic (Claude Code, Copilot CLI and Codex signed in to their own plans) goes into traces. Off, their requests are still counted, without bodies. |
| `capture.terminal_output` | on (default) or off | Whether shared terminals' output is recorded. Off, viewers still see it live; nothing is kept. |
| `days.traces` | days, or forever | Traces (and the chat view built from them). |
| `days.terminal` | days, or forever | Recorded terminal output. |
| `days.records` | days, or forever | Request records: the Requests page, usage reports. |
| `days.sessions` | days, or forever | The session itself, and everything it recorded. |
| `days.audit` | days (at least 90) | The [audit log](audit.md). |

The server's policy also has `allowed_traces` (which capture modes organizations may choose), and `min` and `max` for each kind: what organizations may set. Days are counted from a session's last activity. Off means not written, so there's nothing to delete later.

The single **Keep recordings for** setting from earlier versions (`retention_days`) is the server policy's traces and terminal days.

## Setting them

- **Server settings › Storage** (site admins), or `PUT /api/config/retention`.
- **Organizations › an organization › Data** (its admins, and site admins), or `PUT /api/orgs/<org>/retention` with only what it changes, such as `{"days": {"traces": 7}, "capture": {"traces": "off"}}`. `DELETE` goes back to the server's policy. Anything outside the server's bounds is refused with the reason.
- On a new install from Helm (or any `agent-server db migrate`), `AI_PROXY_RETENTION_SEED` (JSON, the same shape) becomes the server's policy once.

```json
{
  "capture": {"traces": "opt_in", "intercept_bodies": true, "terminal_output": true},
  "allowed_traces": ["off", "opt_in", "always"],
  "days": {"traces": 30, "terminal": 30, "records": null, "sessions": 400, "audit": 365},
  "max": {"traces": 90, "terminal": 90, "records": null, "sessions": null, "audit": null},
  "min": {"traces": null, "terminal": null, "records": 30, "sessions": null, "audit": 90}
}
```

## When a policy applies

A session takes its organization's policy when it starts (its `retention` tag, which agents and clients can't set). Changing a policy later doesn't stretch or shorten what sessions already running or recorded were started under; new sessions get the new one. Tracing a session the policy doesn't allow is refused (`409`).

Deleting is the worker's job, hourly. It looks only at sessions whose next deletion is due, so a large history costs nothing until something in it expires.

## Legal holds

A hold on an **organization**, a **person** (the sessions they started: for an agent started from the console, whoever started it, else the session's owner) or a **session** stops scheduled retention deletion of the covered session data until it is lifted. Manual session deletion and clearing bypass this check in the current implementation. Only an **organization** hold also prevents deletion of that organization’s audit events; person and session holds do not filter audit events by actor or session. Holds are an organization's admins' (and site admins'): **Organizations › Data › Legal holds**, or `POST /api/holds` with `scope` (`org`, `user` or `session`), `target` and `reason`; `DELETE /api/holds/<id>` lifts one. Placing and lifting holds is in the audit log.

## Where recordings are

In the database, or in blob storage (S3 or compatible) with `AI_PROXY_BLOB_URL`; see [Running several server processes](scaling.md#blob-storage). In blob storage they're kept under each organization's prefix, so retention deletes a session's prefix, and an organization's data sits under one prefix (nothing removes that prefix for you). Coordinate bucket lifecycle rules, backups and exports separately: they do not consult application legal holds and can delete held data or retain copies beyond application retention.
