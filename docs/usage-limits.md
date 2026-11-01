# Providers and usage limits

A **provider** is anywhere agents' model calls go. The server treats each one as something people use and something an organization can limit:

| Provider | What it is | Its id |
| --- | --- | --- |
| A model server on a machine | Ollama, llama.cpp, Strata, Docker Model Runner or a container, added as a provider from its machine's page | `backend:<name>` |
| A hosted API | The Anthropic API (Server settings) | `backend:<name>` |
| A kind of subscription | Claude, ChatGPT (Codex) or GitHub Copilot: account agents' calls to that provider's own hosts, whichever plan or sign-in carried them | `subscription:claude`, `subscription:codex`, `subscription:copilot` |

The server counts each person's usage of each provider and lets an organization's admins put token limits on it. For example, in organization `pockets`:

| Person | Provider | Limit |
| --- | --- | --- |
| joey | Claude subscription | 1,000,000 tokens this month |
| joey | Ollama on game-pc | 10,000,000 tokens this month |
| joey | llama.cpp on fold6 | 100,000 tokens a day |

Once joey has used 1,000,000 tokens on Claude this month, the server refuses their Claude Code's calls to Anthropic until the month ends or an admin raises the limit. Their agents on Ollama and fold6 carry on.

## The Providers page

**Agents › Providers** (`/providers`) lists every provider your organization can use: **On machines** (with the machine, whether it's connected, and a link to its page), **Hosted APIs** and **Subscriptions** (with how many of your subscriptions the server has, and a link to [Subscriptions](subscriptions.md)). Each card shows your calls and tokens in the window you choose (the last 5 hours, 24 hours, 7 days, 30 days, or this month in UTC), every limit on you there with how much of it you've used, and **Blocked** with when it frees up once you've reached one.

## How usage is counted

- Every model call recorded by the server counts: its input and output tokens as the provider reported them in its answer, and one call. Calls to a subscription's other hosts (sign-in, telemetry, updates) don't count.
- It counts for the person the session is for: whoever started it from the console (on a machine, the agent runs under the machine's sign-in, but the usage is the person's who asked), otherwise whoever signed in with `agent login` on the computer that ran it.
- It's counted by the hour and kept for 62 days. Rolling windows are whole hours: "the last 24 hours" is the 24 hours up to the current one, inclusive.
- Only what went through this server counts. Use of the same subscription in the provider's own apps, or on computers that don't go through the server, isn't counted.
- Counting starts with this version: calls recorded before it aren't in anyone's usage.

## Setting limits

On **Organizations**, open the organization and its **Usage** tab. Its admins (and site admins) see each person's usage of each provider, with the same windows, filters by person and provider, and who's blocked. Members see only their own rows.

Under **Limits**, an admin sets one with:

- **Person**: someone in the organization, or **Everyone**. Everyone's limit is each person's own, counted separately for each person, not a shared pool. A person's own limit for a window replaces everyone's for that window.
- **Provider**: one the organization can use.
- **Window**: the last 5 hours, 24 hours, 7 days, 30 days, or this month (UTC).
- **Tokens**: a whole number; `1m`, `10m`, `100k` and `2.5b` work too. `0` turns the provider off for them.

A person may have limits for several windows on the same provider (say 200k in 5 hours and 2m a month); all of them apply. Setting a limit again for the same person, provider and window replaces it; **Remove** takes it away.

## What a blocked person sees

When a limit is reached:

- Their agents' calls to that provider answer `429 Too Many Requests` with `Retry-After` (seconds until it frees up) and a body both OpenAI and Anthropic clients read:

  ```json
  {"type": "error", "error": {"type": "rate_limit_error", "code": "usage_limit",
   "message": "Usage limit reached: 1,000,312 of 1,000,000 tokens used on Claude (Claude Code) subscription in this month (UTC). It frees up at 2026-12-01 00:00 UTC. Ask an admin of your organization to raise it."},
   "detail": "..."}
  ```

  For a subscription the call never reaches the provider. The refusal is recorded in the session's requests (`error_type: usage_limit`), so it shows in Requests and Traffic, but it doesn't count as usage, and it isn't taken for the provider's own rate limit on a subscription.
- New sessions on that provider are refused with `429` and the same message: `agent <agent>` says so and doesn't start the agent, and so do **Start an agent** and a machine's **Start an agent**.
- Listing models (`GET /v1/models`, `/api/tags`) still works.

A rolling window frees up when enough of its oldest hours have left it; the month frees up on the 1st (UTC). A call already running when the limit is reached finishes, so a person can go past a limit by the size of their last call.

## API

| Method | Path | Who | What |
| --- | --- | --- | --- |
| GET | `/api/providers` | Signed in | The providers your organization can use, each with `kind` (`local`, `hosted`, `subscription`), `type`, `label`, `machine`, your `usage` by window (`requests`, `input_tokens`, `output_tokens`), the `limits` on you with `used`, `remaining`, `percent_used`, `reached`, `until` and `inherited` (everyone's), and `blocked` (the limit that frees up last, or null). |
| GET | `/api/orgs/{org}/usage` | Its members (their own rows); its admins and site admins (everyone's) | `rows` (one per person and provider with usage or a limit: `usage`, `limits`, `blocked`), `providers`, `users`, `windows`, `manage`, and for admins the organization's `limits`. Another organization's is `404`. |
| PUT | `/api/orgs/{org}/limits` | Its admins, site admins | Sets one limit: `{"user": "joey" or "*", "provider": "backend:ollama-game-pc", "window": "month", "tokens": 10000000}`. Answers the usage report. `400` for someone outside the organization, a provider it can't use, an unknown window or a negative number; members get `403`. |
| DELETE | `/api/orgs/{org}/limits?user=&provider=&window=` | Its admins, site admins | Removes one limit. `404` if there's none. |

Limits are kept in the database (`usage_limits`), usage in `provider_usage` (hourly, per organization, person and provider). Neither is part of `agent-server config export`: they're an organization's, like its users.
