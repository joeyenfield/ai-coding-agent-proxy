# The console

The console is the server's web app. Open the server's address in a browser (`http://<server>:9999`, or your HTTPS address). The Android app shows the same console in a WebView, so everything here works on a phone too.

The screenshots come from the demo (`python scripts/screenshots/demo.py`), which runs a server with made-up machines, agents and traffic.

- [Layout](#layout)
- [Signing in](#signing-in)
- [Overview](#overview)
- [Agents: Start an agent](#start-an-agent)
- [Agents: Running agents](#running-agents)
- [Server: Live](#live)
- [Server: Traffic](#traffic)
- [Server: Requests](#requests)
- [Server: Sessions](#sessions)
- [Server: Server settings](#server-settings)
- [Server: Organizations](#organizations)
- [Server: Downloads](#downloads)
- [Machines](#machines)
- [A machine's page](#a-machines-page)
- [Pairing a phone](#pairing-a-phone)
- [Documentation](#documentation)

## Layout

The rail on the left has three parts, in the order a request flows:

| Part | Pages | What it's for |
| --- | --- | --- |
| **1 Agents** | Start an agent, Running agents | Start a coding agent through the server, then watch it and type into it from any device. |
| **2 Server** | Live, Traffic, Requests, Sessions, Server settings, Organizations, Downloads | The hub: allowed agent requests pass through it and are recorded. |
| **3 Machines** | All machines, Add a machine | Open **All machines** to search and select a computer. Individual machines aren't listed in the rail. |

- The small label above each page's title is its breadcrumb ("Machines › build-box").
- The footer of the rail links to **Downloads** and **Documentation**, and shows how many sessions are open, the server's address and its version.
- A badge on **Running agents** counts agents waiting for you. A badge on **Live** counts requests in flight. A badge next to a machine means it runs an older agent-machine than the server.
- When an agent asks a question (a permission prompt, say), a banner at the top of every page shows it. **Answer** opens its terminal; **Later** hides it until the agent asks something new.
- Below 900 px wide the rail becomes a top bar with a menu. Opening the menu adds a history entry, so Back (Android's included) closes it.
- The console follows your system's light or dark theme.

![The Overview in a phone-sized window](screenshots/overview-phone.png)

## Signing in

The console always requires sign-in. **Sign in** redirects to the separate OIDC issuer using authorization code + PKCE; passwords are entered only there. Access renews automatically through host-only HttpOnly cookies. **Downloads** and documentation remain public.

An unavailable authentication service shows an error, never an unsigned console. See [OIDC authentication](oidc.md).

![OIDC sign-in](screenshots/login.png)

![Sign-in on a phone](screenshots/login-phone.png)

## Overview

![Overview](screenshots/overview.png)

The landing page (`/`) shows the whole system at a glance:

- **The map**: agents on the left, the server in the middle, and each provider (with its machine) on the right. Lines light up while requests flow.
- **Agents, Server, Machines** cards: open sessions and installed agents; requests, tokens read and written and requests in flight; machines online. Each card links to its pages.
- **Traffic by route**: requests, tokens and last use for each of the three routes (your accounts, local models, hosted API).
- **Generation speed**: tokens per second for each recent request.
- **Streaming now** and **Latest model calls**: requests in flight and the most recent ones, each linking to its details.

If the console can't reach the server, the page says so (**Can't reach the proxy**).

## Start an agent

![Start an agent, for an account agent](screenshots/agents.png)

`/agents` lists every agent the server knows (from `config/agents.yaml`), grouped by route: **Your accounts**, **Local models** and **Hosted API**. Each shows whether it's installed on the server's computer and which API it speaks. Pick one to see its launch panel:

- **Install**: the install command for Linux, macOS, Windows or WSL. The console remembers your choice.
- **Provider and model** (local and hosted agents): the backends this agent can use, with the one the server picks by default, and the models on that backend (aliases from `models.yaml` included). An offline backend says why.
- **Capture full request and response payloads**: saves every request and reply in full (`--trace`). Account agents redact credentials.
- **Watch and type into it from Running agents** (`--control`): shares the agent's terminal with the server.
- **MCP servers**: the MCP servers your machines share that you may use. Ticking one adds `--mcp` to the command. Agents that can't take MCP servers (Aider) say so. See [MCP servers](mcp.md).
- **Sharing** (with accounts): **Only me**, a team or an organization, and whether they may type into it.
- **Run it from your project folder**: the full `agent` command, ready to copy.
- **Set it up by hand instead**: opens a session now and prints the environment for the agent in PowerShell or bash, for when you can't use the launcher. Started this way, its terminal isn't shared. For MCP servers it lists the URLs to add to the agent's own settings.

![Start an agent, for a local-model agent with an MCP server chosen](screenshots/agents-ollama.png)

To start an agent on one of your machines from the console instead, use **Start an agent** on that machine's page (see [Agents tab](#agents)).

## Running agents

![Running agents](screenshots/terminals.png)

`/terminals` lists the agents whose terminals are shared with the server (started with `--control`, or from a machine's page). For each it shows the agent, project, machine, how long it's been running and its state:

| State | Meaning |
| --- | --- |
| **Working** | The agent is printing output. |
| **Waiting** | It's quiet, probably waiting for your next prompt. |
| **Needs you** | It asked a question, such as a permission prompt. The question is shown. |
| **Not connected** | The launcher's connection dropped; the terminal's history is still there. |
| **Connecting** | The viewer is joining. |

Ended terminals stay listed with their exit status until their session is deleted.

### A terminal

![A shared terminal](screenshots/terminal.png)

Opening one (`/terminals/<session>`) shows the agent's live terminal (xterm.js) with its history:

- **Type into it** in the message box and **Send**. Text and Enter go as separate keystrokes, and multi-line text is sent as a paste, so agents read it the way they would from a keyboard.
- **Keys** for phones: Esc, Enter, arrows, Scroll ↑/↓, PgUp/PgDn, Tab, ⇧Tab, Ctrl+C, and 1, 2, 3, y, n for answering prompts. Scroll ↑/↓ scroll the agent's own history in full-screen agents (Claude Code, OpenCode, Qwen Code) and the terminal's elsewhere.
- **A−/A+** change the font size; **Fit width** sizes it to the window; **Light background** switches the terminal's colours; **Latest** jumps to the bottom.
- **Session traffic** opens the session's requests on Traffic.
- If the session is shared with you to watch only, the input is off.

![A shared terminal on a phone](screenshots/terminal-phone.png)

### Use it from your phone

On a phone, open the console and sign in through OIDC. Legacy pairing codes no longer grant access.

## Live

![Live](screenshots/live.png)

`/live` streams model calls as they happen: one card per request in flight, with the reply, reasoning and tool calls as they arrive, time to first token and speed. **Just finished** keeps the last few. Each card opens its full details (headers, the request, the payload sent to Ollama and the raw stream), and **Open in request history** goes to the saved request.

Account agents' other calls (sign-in, telemetry, MCP) aren't shown here; they're on Traffic. Set `AI_PROXY_LIVE=0` on the server to stop streaming request text (counts still stream).

## Traffic

![Traffic](screenshots/traffic.png)

`/traffic` lists recorded requests through the server: model calls on the proxy endpoints, permitted account-agent requests to public services, and calls to shared MCP servers. The HTTPS proxy refuses private and reserved destinations.

- **Search** by service, host, path, agent or model, pick a **session**, and tick **Failed only**.
- **Request types** filter the list: Model call, Token count, Model list, MCP, Account, Sign-in, Settings & flags, Telemetry, Updates, Connection and Other, each with its count. What each type covers is in [What the requests are](agents.md#what-the-requests-are).
- **Group repeats** folds back-to-back identical requests (agents poll some endpoints) into one row.
- Each row shows the type, service, method and path, the session, its status, timing and size; requests in flight are marked **pending**.
- Selecting a row opens the inspector on the right: timings, tokens and sizes, and tabs for the **Conversation** (a readable view of the messages), the **Request**, the **Response**, what was **Sent to Ollama** (for translated calls), the request and response **Headers** (credentials redacted) for captured HTTPS, and **Metadata**. **Full screen** opens it alone; **Download full trace** saves the JSON.
![Traffic: the headers of a captured request, credentials redacted](screenshots/traffic-headers.png)

![Traffic filtered to model calls](screenshots/traffic-models.png)

- **Still in memory** lists requests from before the page loaded that the server still holds in memory.

## Requests

![Requests](screenshots/requests.png)

`/requests` is the model-call history across all sessions: model, agent, provider, duration, output tokens and speed.

- **Search** by model, agent, endpoint or ID; filter by **session**, **route** (accounts, local models, hosted) and **With payload**, **Without payload** or **Failed**.
- **Include non-model requests from account agents** adds their sign-in, telemetry and MCP calls, which are hidden by default (Traffic shows them too).
- Selecting a row shows the same inspector as Traffic. Each request also has its own page (`/requests/<session>/<request>`):

![One request](screenshots/request.png)

## Sessions

![Sessions](screenshots/sessions.png)

`/sessions` lists every session: agent, model, provider, when it started, requests and tokens, and whether payloads are saved. Selecting one shows its **Latest requests** and its actions:

- **Capture full request and response payloads**: turn saving payloads on or off for the rest of the session.
- **Review requests**, **Traffic**: open its requests on those pages.
- **Download JSONL**: its telemetry, one request per line.
- **End session**: closes an open session; its agent can no longer use it.
- **Clear data**: deletes its recorded requests and payloads but keeps the session.
- **Delete**: deletes the session and everything recorded for it. A session with requests in flight can't be cleared or deleted.
- **Delete all sessions** deletes ended sessions, empties open sessions idle for more than 15 minutes, and leaves recently active ones running. Only your own.
- **Open a session manually**: for other tools. Name the agent, model and backend, choose whether to capture payloads, and **Open session**; it shows the OpenAI, Anthropic and Ollama URLs to point the tool at.

### A session

![A session](screenshots/session.png)

`/sessions/<id>` shows the session's details (agent, model, provider, machine, project, started, last traffic, tokens read and written, payloads, its OpenAI and Anthropic URLs), its sharing for its owner, and tabs:

- **Chat**: the session's model calls read back as conversations. Each conversation is a thread of turns (the main one first, sub-agents as their own threads), with long tool output shortened until you open it. Replies link to the request that made them.
- **Requests**: its requests, as on Traffic.

**Open in Traffic** shows its requests there, and **Streaming now** shows what's in flight.

## Server settings

![Server settings](screenshots/settings.png)

`/settings` changes `config/backends.yaml` (admins only, with accounts):

- **Server**: **Listen host**, **Listen port**, and **Address agent machines use** (`public_url`). A changed address needs a restart.
- **Providers**: every backend, with its type, address and machine. Choose the **Default**, or remove one. **Add hosted API** adds an Anthropic API backend (name, type, URL). Local providers can't be added here: they come from machines (see [Providers](providers.md)).
- **Restart the server**: restarts it with the settings on disk once requests in flight finish, and says where it comes back.

Saving rewrites `backends.yaml`, so comments in it are lost. **API reference** links to the server's OpenAPI pages (`/docs`).

## Organizations

![An organization and its teams](screenshots/organization.png)

`/organization` separates the directory from your own account. Its tabs and selected organization and team stay in the URL, so you can link to them:

- **Organizations**: search and select an organization. Its **Teams** tab lists its teams; open one to add or remove members, create a user in it, or rename or delete it in **Team settings**. **Members** manages everyone in the organization, including users not assigned to a team. Add existing users before placing them in teams, or create an account directly in either view. **Settings** renames or deletes the organization.
- **User directory**: search accounts and filter by organization, including **No organization** for unassigned users. Admins can create accounts, **Edit user** to change their server role, memberships or password, and **Delete account**. Removing someone from a team or organization is separate from deleting their account.
- **Your account**: your identity and OIDC sign-in instructions. CLI sign-in uses device approval; scripts use explicitly provisioned service clients ([OIDC](oidc.md)). Admins reset account passwords in User directory or with `agent-server user passwd`.

Members see only their organizations and the people in them; all management controls require a server admin. Removing an organization membership also removes that user's team memberships there. Deleting a team or organization keeps people's accounts. Destructive changes ask for confirmation.

See [Accounts and sharing](accounts.md).

## Downloads

![Downloads](screenshots/downloads.png)

`/get` is public, so a new machine can get the programs before it signs in:

- **Install with one command**: the one-line installer for **Linux, macOS, FreeBSD** (`curl ... | sh`) and **Windows (PowerShell)** (`irm ... | iex`), for the **Client** (`agent`) or **agent-machine**. They download this server's build for the computer, check it and sign it in.
- **Get them**: every archive and package of this server's build, by platform, with sizes and SHA-256. **The programs on their own** lists the bare binaries.
- **Python client and agent-machine**: the wheel, for computers without a Go build.
- **Android app**: the APK.

If nothing has been built (`scripts/build-dist`), it says **Nothing built yet**. See [Deployment](deployment.md#what-the-server-hands-out).

## Machines

![Machines](screenshots/machines.png)

`/machines` lists the fleet in rows: **Name** (with online/offline state and owner), **Architecture** (with operating system), **Last contact** (hover for the full date and time), and **Functions**. Open a machine's name to manage it.

Each row marks **Agents**, **Terminals**, **Model servers**, **Server management**, **Containers**, **Updates**, **Tunnel** and **MCP servers** as Enabled or Disabled. These are the machine's reported settings, not your personal permissions or a promise that an offline machine is usable. Model servers and MCP servers are enabled when the machine reports servers; the other functions follow its switches. An update badge shows when a newer build is available. Narrow screens show the same fields stacked.

- **Search machines**: filter by name, machine id, hostname, address or owner. Search ignores case; clearing it shows the whole list again.
- **Add a machine**: the commands to install and sign in agent-machine on Linux or macOS and on Windows (PowerShell). **Add another machine** shows them again when the fleet isn't empty.
- **Or run a sandbox machine in a container**: the Docker or Podman command for the sandbox image, which runs agents in a container (see [Machines](machines.md#a-sandbox-machine-in-docker)).
- **Removed**: machines taken off the server, which you (their owner or an admin) can allow back.

## A machine's page

![A machine's summary](screenshots/machine.png)

`/machines/<id>` has a header with the machine's name, state and system, **Start an agent** and **Open a terminal** (when you may use them), and, when the machine runs another build than the server, **Update now** (with **Update agent there too**) for its owner or an admin. Below it, a tab per function. Tabs you can't use aren't shown.

### Summary

**What it supports**: each feature (launches, terminals, model servers, containers, updates, MCP) and whether it's on there and open to you, with the command that turns it on. **Details**: machine id, computer name, owner, address, system, agent-machine version and protocol, launch folders, first seen and last contact. **Remove from this server** (owner or admin) disconnects it and refuses it until it's allowed back.

### Agents

![A machine's Agents tab](screenshots/machine-agents.png)

**Running agents** on the machine (launched from here or started there by hand), **Installed there** (the server's agents found on its `PATH`), **Start an agent** and **Started from here** (each launch: who, which agent, which folder, its session or why it failed).

**Start an agent** picks the agent, the provider and model, the folder (browse the folders inside its launch folders, or name a new one), MCP servers, sharing and payload capture, then starts it in a shared terminal and opens it. It needs `agent-machine launches on` and a launch folder there.

### Terminal

![A machine's Terminal tab](screenshots/machine-terminal.png)

**Open a terminal** starts a shell on the machine in one of its launch folders, in a terminal only you see. It needs `agent-machine commands on` there.

### Model servers

![A machine's model servers](screenshots/machine-model-servers.png)

The model servers it runs (Ollama, Strata, Docker Model Runner, and the containers it runs for the server), each with its address, version and whether it's a provider yet. A model server that isn't a provider has **Add as a provider** (admins); after that, every agent that speaks its API can use it.

### One tab per model server

![An Ollama model server](screenshots/models.png)

Each model server has its own tab, named after its provider (or `Ollama :11434` until it is one). Everything here is done by the machine:

- **Ollama**: **Models** (each model's size, whether it's loaded and where, the agent requests it served, and load, unload and delete), **Aliases** (the `models.yaml` aliases that point at it, with their settings) and **Find and pull** (search the Ollama library and Hugging Face, or pull by name, with progress).
- **Strata**: **Overview** (state, profile, engine, uptime, model, context, prompt and generation speed, speculative decoding), **Hardware**, **Models** (load and unload), **Connect agents** (the agent commands for it) and **Log** (server and engine logs, key blanked).
- **Docker Model Runner**: **Models**, **Get models** (search Docker Hub's `ai/` models and their tags, or pull by name) and **Connect agents**.

Changing models needs `agent-machine servers on` there; without it the tab shows the models but doesn't change them.

### Ollama server

![The machine's Ollama settings](screenshots/ollama-server.png)

For the Ollama installed on the machine: **Start**, **Stop** or **Restart** it (with `servers on`), **Settings** (the `OLLAMA_*` settings agent-machine starts it with, such as context length, parallel requests, flash attention and KV cache type, with warnings), **Environment** (what Ollama starts with, plus **Extra variables**) and **Log**.

### Containers

![Containers a machine runs](screenshots/containers.png)

Strata and Ollama containers the machine runs for the server. Each shows its phase (Not created, Pulling image, Starting, Loading model, Ready, Unhealthy, Stopping, Stopped, Failed), settings and GPU, with start, stop, restart, switch (stop the others holding its GPU), logs, edit and delete. **Add a container** picks the engine and its settings; **Shared model storage** is the volume or folder its models live in; **The command its machine runs** shows the exact `docker run`. See [Providers](providers.md#containers-machines-run).

### Load

![A machine's load](screenshots/hosts.png)

The machine's CPU, memory and GPU use over time, sampled there, and **Loaded on** each Ollama: which models are in memory.

### MCP servers

![A machine's MCP servers](screenshots/machine-mcp.png)

The MCP servers the machine shares, by name, with how each runs (a command or a URL) and whether it's installed there. Its owner sees the `agent-machine mcp add` commands. See [MCP servers](mcp.md).

### Access

![A machine's Access tab](screenshots/machine-access.png)

For its owner and admins: which organizations and teams may use each feature (see it, start agents, open terminals, change models, run model servers, update it, use MCP servers), and which of the machine's own switches each also needs. See [Machines](machines.md#who-may-use-a-machine).

## Pairing a phone

Pairing keys and codes no longer grant access. Open the console on the phone and sign in through OIDC. Terminal access follows session sharing. Old `/pair` links redirect to Running agents after sign-in.

## Documentation

`/help` shows this documentation in the console: the pages of `docs/` on the server, with their pictures, and **API reference (OpenAPI)** for the server's `/docs`. Links between pages stay in the console, and each heading has its own address (`/help/mcp#how-it-works`). Diagrams are drawn from the pages' Mermaid blocks, in the console's light or dark theme. Like Downloads it's public, so it can be read before signing in; the sign-in page links to it.

The server reads the pages from the `docs` folder of its checkout (the Docker image has them in `/app/docs`), or `AI_PROXY_DOCS_DIR`. A server installed without them says so on the page.
