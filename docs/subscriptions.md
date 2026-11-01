# Subscriptions

Your own AI plans (Claude Pro or Max, ChatGPT Plus or Pro through Codex, GitHub Copilot), kept on the server as **provider accounts**. A provider account is not your sign-in to the server: that's your account in [Accounts and sharing](accounts.md). It is one person's subscription with an outside provider, which runs on the machines its owner assigns it to.

With subscriptions on the server you can:

- see every plan you have, which machines run it, and whether each is ready;
- start Claude Code, Codex and Copilot CLI on a machine from the console with one of your plans, without signing in there again;
- see how much went through the server for each plan, and an estimate of what's left of a limit you declare;
- let the server pick a plan that isn't throttled or used up, and fall back to your own local models.

The provider's own limits always apply. The server never works around them, and never uses a pay-as-you-go API instead of your plan unless you start such an agent yourself.

## How each provider signs in

How a subscription signs in is the provider's business, and they differ. The server supports two modes:

| Mode | What it means | Claude Code | Codex CLI | Copilot CLI |
| --- | --- | --- | --- | --- |
| **Signed in on its machines** (`runner`) | The provider's CLI is signed in on each machine, and stays signed in there. The server only knows that the machine says so. | Yes (the only mode) | Yes (the only mode) | Yes |
| **Managed token** (`token`) | A token the provider documents for headless use is kept on the server, encrypted, and handed to launches on the machines you assign. | No | No | Yes: a fine-grained personal access token with the **Copilot Requests** permission, as `COPILOT_GITHUB_TOKEN` |

- **Claude Code**: sign in with `claude` on each machine. Your Claude subscription sign-in is never collected, stored, copied or relayed by the server.
- **Codex**: sign in with `codex login` on each machine (`codex login --device-auth` without a browser). The server doesn't copy Codex's `auth.json` between machines.
- **Copilot CLI**: sign in with `copilot` and `/login` on each machine, or store a fine-grained personal access token. Classic tokens (`ghp_...`) don't work with Copilot CLI and are refused. A machine that gets the token can use your Copilot subscription, so assign it only to machines you trust.

## Adding a subscription

From the computer where the provider's CLI is signed in (it must be one of your [fleet machines](machines.md) for agents to start there from the console):

```bash
agent accounts detect                           # which provider CLIs are here, and whether each is signed in
agent accounts enroll claude --plan "Max 5x"     # register this machine as one that runs your Claude plan
agent accounts enroll codex --name "ChatGPT Plus"
agent accounts enroll copilot --token-stdin     # store a Copilot token instead (asks before storing it)
agent accounts list                             # your subscriptions, their state and machines
```

Enrolling finds the subscription you already have with that provider and name rather than making another, so run it again on each machine. It sends only whether the CLI is signed in and its version, never the sign-in.

Or on the console's **Subscriptions** page (under Agents): **Add a subscription**, then assign it to machines and, for a token, store it there.

### `agent accounts`

| Command | What it does |
| --- | --- |
| `agent accounts list` | Your provider accounts on the server, their state and the machines they run on. |
| `agent accounts detect [--json]` | The provider CLIs on this computer, their versions, and whether each is signed in. `--json` is what agent-machine reports. |
| `agent accounts status` | Both of the above. |
| `agent accounts enroll <claude\|codex\|copilot>` | Register this computer as a machine the subscription runs on. Options: `--name NAME`, `--plan PLAN`, `--machine ID` (default: your machine with this hostname), `--force` (enroll even if the CLI doesn't look signed in), `--token-stdin` (Copilot: read a token from standard input and store it, encrypted), `--yes` (don't ask before storing it). |

How a sign-in is detected, without reading any credential:

- Claude Code: whether `.credentials.json` exists in `~/.claude` (or `CLAUDE_CONFIG_DIR`); it isn't opened. On macOS the sign-in is in the Keychain, so it says it can't tell. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` set means an API key, not a subscription.
- Codex: `codex login status`, Codex's own command. Only whether it says ChatGPT or an API key is kept.
- Copilot CLI: it has no status command; a token in `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` counts as signed in, otherwise it can't tell.

agent-machine runs `agent accounts detect --json` every five minutes and sends the result with its hello, so each machine's page shows it under **Agent accounts**.

## Starting an agent with a subscription

On a machine's page, **Agents › Start an agent**, choose an account agent (Claude Code, Copilot CLI or Codex with your account), then **Subscription**:

- **The machine's own sign-in**: whatever the CLI is signed in to there. Only the machine's owner has this choice.
- **Choose automatically**: the best of your subscriptions that runs on this machine now (see [Choosing a subscription](#choosing-a-subscription)).
- One of your subscriptions by name.

The same through the API: `POST /api/machines/{id}/launch` with `account_id` (an id, or `auto`). By hand, on a machine where the CLI is signed in: `agent claude-account . --account <id>`.

Someone the machine's owner lets start agents there (a `launch` grant) can't spend the owner's subscription by doing so: an account agent started by anyone but the machine's owner needs a subscription they may use. Its owner can name others who may start agents with it (**Others who may start agents with it**); they never see or get its sign-in.

A launch is refused (409) when the subscription is turned off, isn't assigned to the machine, the machine hasn't reported the CLI or it isn't signed in there, the provider is throttling it or said its limit is used up, or (for a token) none is stored or handing it to that machine is turned off.

### How a managed token reaches the agent

```mermaid
sequenceDiagram
  participant C as Console
  participant S as agent-server
  participant M as agent-machine
  participant A as agent (launcher)
  participant P as Copilot CLI
  C->>S: POST /api/machines/{id}/launch (account_id)
  S->>S: checks; a one-time grant for this launch
  S->>M: launch (the account's id, no token)
  M->>A: agent copilot-account ... --account <id>
  A->>S: POST /api/agents/copilot-account/sessions (account)
  A->>S: POST /api/sessions/{id}/provider-credential
  S-->>A: the token, once
  A->>P: runs it with COPILOT_GITHUB_TOKEN in its environment
```

The token is decrypted only for a launch the server itself sent, to a machine the subscription is assigned to, within two minutes, once. It goes into the agent's environment only: never a file, a command-line argument, the session's record, the launcher's output or a trace (the intercept already redacts `Authorization`).

## Usage and remaining balance

The providers don't offer the server a supported way to read your plan's meter, so the server says what it knows and where it came from:

| Figure | Source | How far to trust it |
| --- | --- | --- |
| Requests and tokens in the last 5 hours, 24 hours, 7 days, 30 days and this month (UTC) | `observed`: model calls of the subscription's sessions recorded by this server, to the provider's own hosts | Exact for this server's traffic; use on other computers or in the provider's apps isn't counted |
| Remaining | `estimate`: a limit you declared (for example 225 requests per 5 hours, or 300 requests a month) minus the observed use in that window | Low: an estimate, shown as one |
| Throttled until, limit used up until | `observed`: the provider's `429` answers and their `Retry-After` | Medium: it's what the provider said |
| The provider's own numbers | `reported` | None is wired up yet, so this is empty |

These figures are the subscription's own. Separately, an organization's admins can limit how many tokens each person uses on each kind of subscription, and block them once they've used it: see [Providers and usage limits](usage-limits.md).

Without a declared limit, the remaining balance is **unknown**, not a made-up percentage. Set one on the subscription's **Usage and settings**, with when it warns (`warning_percent`, 75 by default: the state becomes *Near its limit*) and when automatic choices stop using it (`stop_percent`, 90).

A `429` from the provider with a short `Retry-After` (under 15 minutes) is a throttle: the subscription is *Rate limited* until then. A longer one means its window is used up: *Limit used up* until then. A 429 without `Retry-After` cools it down for a minute. 429s from other hosts (telemetry, package registries) don't count. **Forget the limits seen** clears them early.

## States

| State | Meaning | New launches |
| --- | --- | --- |
| Available | A machine it's assigned to is connected and signed in | Yes |
| Near its limit | Past `warning_percent` of a declared limit (estimated) | Yes |
| Sign-in unknown | A machine is ready, but can't tell whether the CLI is signed in | Yes |
| Rate limited | The provider is throttling it | No, until the time shown |
| Limit used up | The provider said its window is used up | No, until the time shown |
| Needs signing in | The CLI is signed out (or on an API key), or no token is stored | No |
| No machine ready | Not assigned, or none of its machines is connected or has the CLI | No |

**Turned off** (by its owner, or an admin of its organization) stops new launches whatever the state.

## Choosing a subscription

`POST /api/provider-accounts/plan` (and `account_id: "auto"` on a launch) picks, for a new task, the subscription, agent and machine that should take it:

1. Only subscriptions you may use, assigned to a connected machine that takes launches, with the agent installed and the folder inside its launch folders.
2. Available first, then near its limit, then sign-in unknown (`allow_unknown`, on by default); never one throttled, used up, signed out or offline, and never one past its `stop_percent` (estimated).
3. Then the subscription's **priority** (lower first), the agents in the order you asked for, and the most left.
4. If none can, the `fallback` agents you name, but only local-model agents (Qwen Code, OpenCode, ...): a paid API is never chosen automatically.

The answer lists the choice, every candidate, and why the others were skipped. Work is never moved to another subscription mid-task: a choice is made when a task starts.

## Who may do what

| Who | See it | Start agents with it | Change it, assign it, store or revoke its token | Turn it off |
| --- | --- | --- | --- | --- |
| Its owner | Yes | Yes | Yes | Yes |
| Someone its owner named | Yes | Yes | No | No |
| An admin of its organization | Its metadata | No | No | Yes |
| A site admin | Its metadata | No | No | Yes |
| A machine it's assigned to | No | Runs the launches the server sends | Gets the token once per such launch | No |
| Anyone else, or another organization | No (404) | No | No | No |

A subscription can only be assigned to its owner's own machines: sharing a machine with others doesn't hand them your plan. Nobody, site admins included, can read a stored token back through the API. Removing a machine from the server, or the assignment, stops new launches there. Every enrollment, assignment, token stored, handed over or revoked, and limit seen is in the subscription's **History** (owner and admins).

## The vault

Managed tokens are encrypted with AES-256-GCM under a master key the database never holds: `AI_PROXY_VAULT_KEYS` (or the file `AI_PROXY_VAULT_KEYS_FILE` names, for container secrets), a comma-separated list of `<version>:<32 bytes, base64>`. Without it, storing a token is refused. Each token is bound to its subscription, so a database row copied to another doesn't decrypt.

```bash
agent-server vault key                 # a new key: add it to AI_PROXY_VAULT_KEYS
agent-server vault status              # which key versions are set, and how many tokens each encrypts
agent-server vault rotate              # re-encrypt every token with the newest key
```

To rotate: add a newer key (keep the old one listed), restart, run `agent-server vault rotate`, then drop the old key. Database backups hold only ciphertext; keep the vault key apart from them. `agent-server config export` has no tokens.

## Not done yet

- Containers that run a coding agent with a subscription (agent-runner containers). Today a subscription runs on fleet machines themselves; `/api/containers` is for model servers only.
- Handing a task from one agent to another when a subscription runs out (structured checkpoints, linked sessions).
- A provider's own usage meter, where one is documented for this.
- Moving a Codex sign-in between machines. Codex's sign-in refreshes itself on each machine that holds it, so each machine signs in.
