# AI Coding Agent Proxy

Run terminal coding agents (Claude Code, GitHub Copilot CLI, OpenAI Codex, Qwen Code, OpenCode, Aider, Goose) through one server and see everything they do. Every model call is recorded with its prompt, reply, tokens and timing. For agents signed in to their own accounts, every other request is recorded too: sign-in, settings, MCP, telemetry and update checks.

![The console's Overview: agents, the server and the machines that answer them](docs/screenshots/overview.png)

**[Read the documentation →](docs/index.md)**

## What it does

- **Records everything agents send.** Live streams, every request to any host, model-call history with full payloads, and sessions read back as conversations.
- **Three routes to a model, mixed as you like.**
  - **Your accounts**: Claude Code, Copilot CLI and Codex on your own plans, captured in transit.
  - **Local models**: any agent on your own Ollama, Strata or Docker Model Runner, with Ollama requests translated and tuned per model.
  - **Hosted API**: requests passed through to the Anthropic API unchanged.
- **A fleet of machines.** Every computer running `agent-machine` supplies its model servers, containers and MCP servers to agents anywhere, through its own connection to the server, so it needs no open port. You can also start agents and open terminals on it from the console.
- **Agents from your phone.** Share an agent's terminal (`--control`), then watch it and type into it from a browser or the Android app, and answer its permission prompts.
- **One person or a team.** It works as a single-user tool. Add users and it becomes a team server, with organizations, teams, private or shared sessions, and access grants per machine.

## Quick start

On the server's computer, from this checkout (Python 3.11+, Node 20+), or with `docker compose up -d`:

```bash
pip install -e "./server[dev]"
agent-server                         # the console on http://127.0.0.1:9999
```

On each computer where you run agents, install `agent` from the server's **Downloads** page (`http://<server>:9999/get`), or with the one-line installer it serves, then:

```bash
agent login http://server:9999       # once; not needed on the server's own computer
agent --list                         # the server's agents, and which are installed here
agent claude-account .               # Claude Code on your claude.ai login, everything recorded
agent qwen . --model qwen3.6:35b     # Qwen Code on a local model
```

On each computer that runs models (or MCP servers) for agents:

```bash
agent-machine login http://server:9999
agent-machine start
```

Then open the console, add its model server with **Add as a provider**, and start agents against it. [Getting started](docs/getting-started.md) walks through all of it.

## Documentation

| | |
| --- | --- |
| [Overview](docs/index.md) | The system in one page, every feature and where it's documented |
| [Getting started](docs/getting-started.md) | From nothing to a first recorded agent session |
| [Architecture](docs/architecture.md) | How it works inside |
| [Agents](docs/agents.md) | The `agent` command, every supported agent, what the requests are |
| [MCP servers](docs/mcp.md) | Sharing MCP servers from machines with agents |
| [Machines](docs/machines.md) | The fleet and `agent-machine` |
| [Providers](docs/providers.md) | Ollama, Strata, Docker Model Runner, containers, hosted APIs |
| [The console](docs/console.md) | Every page of the web console |
| [Accounts and sharing](docs/accounts.md) | Users, organizations, teams, sharing, phones |
| [Configuration](docs/configuration.md) | Config files, environment variables, server commands |
| [API](docs/api.md) | Every endpoint and WebSocket |
| [Deployment](docs/deployment.md) | Docker, HTTPS, installers, packages, updates |
| [Development](docs/development.md) | Building, testing, releasing |

## The parts

| Part | Folder | What it is |
| --- | --- | --- |
| Server | `server/` | `agent-server` (Python): the API, recording, routing, accounts; serves the console |
| Console | `ui/` | The web console (React) |
| Client | `client/` | `agent` (Go): the launcher and each computer's `agent-proxy` |
| Machine | `machine/` | `agent-machine` (Go): keeps a computer in the server's fleet |
| Android app | `android/` | Your servers' consoles on your phone |
| Python client and machine | `python/` | `agent` and `agent-machine` for computers without a Go build |

[`AGENTS.md`](AGENTS.md) is the guide for changing the code.
