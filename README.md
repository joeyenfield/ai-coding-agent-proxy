# AI Control Mesh

**Your AI agents. Your machines. One control plane.**

AIControlMesh is an open-source, self-hosted control plane for AI coding agents, LLM providers, MCP tools, and distributed machine fleets. Route supported agents to compatible local or hosted models, share tools, manage machines, and follow sessions from one console. Enable tracing to save full prompts and responses; request metadata, tokens, and timings are recorded separately.

![The console's Overview: agents, the server and the machines that answer them](docs/screenshots/overview.png)

**[Website](https://aicontrolmesh.com/) · [Try the local quickstart](docs/quickstart.md) · [Documentation](docs/index.md) · [MIT license](LICENSE)**

## What it does

- **Understand agent activity.** Follow live streams, request metadata, tokens and timings. Enable full tracing to save payloads and read sessions back as conversations. Account-agent capture permits public destinations; private and reserved addresses are refused.
- **Three routes to a model, mixed as you like.**
  - **Your accounts**: Claude Code, Copilot CLI and Codex on your own plans, captured in transit.
  - **Local models**: supported agents on compatible Ollama, Strata or Docker Model Runner backends, with Ollama requests translated and tuned per model.
  - **Hosted API**: requests passed through to the Anthropic API unchanged.
- **A fleet of machines.** Every computer running `agent-machine` supplies its model servers, containers and MCP servers to agents anywhere, through its own connection to the server, so it needs no open port. You can also start agents and open terminals on it from the console.
- **Agents from your phone.** Share an agent's terminal (`--control`), then watch it and type into it from a browser or the Android app, and answer its permission prompts.
- **One person or a team.** Mandatory OIDC sign-in, organizations, teams, private or shared sessions, and access grants per machine.

## Quick start

Install Git and Docker with Compose (or Podman with a Compose provider), then:

```bash
git clone https://github.com/joeyenfield/ai-coding-agent-proxy.git
cd ai-coding-agent-proxy
docker compose -f quickstart/compose.yaml up -d
```

For Podman, replace `docker compose` with `podman compose`. This starts the server and console, sign-in service, PostgreSQL, two fleet machines, and Ollama using the project's published images from ghcr.io. Open http://localhost:19999 and sign in with **admin** / **secret**. Add machine-1's Ollama as a provider, pull a model, and launch an agent using the [guided quickstart](docs/quickstart.md).

**Local demo only:** known credentials, loopback ports 19999/20000, and no model downloaded automatically. For a real install, configure your own credentials, HTTPS and OIDC using [Deployment](docs/deployment.md) and [Security](docs/security.md). The public website's existing console link requires an account for that deployment; it is not the local quickstart.

On the server's computer, from this checkout (Python 3.11+, Node 20+), or with `docker compose up -d`:

First configure the separate **`agent-auth`** service at your own issuer URL, HTTPS, callbacks and the shared introspection secret: [OIDC authentication](docs/oidc.md). API access requires OAuth access tokens, including on loopback. Replace `api.example.com` below with your server's HTTPS address.

```bash
pip install -e "./server[dev]"
agent-server                         # the console on http://127.0.0.1:9999
```

On each computer where you run agents, install `agent` from the server's **Downloads** page (`http://<server>:9999/get`), or with the one-line installer it serves, then:

```bash
agent login https://api.example.com       # approve the device code; required on every computer
agent --list                         # the server's agents, and which are installed here
agent claude-account .               # Claude Code on your claude.ai login, everything recorded
agent qwen . --model qwen3.6:35b     # Qwen Code on a local model
```

On each computer that runs models (or MCP servers) for agents:

```bash
agent-machine login https://api.example.com
agent-machine start
```

Then open the console, add its model server with **Add as a provider**, and start agents against it. [Getting started](docs/getting-started.md) walks through all of it.

## Documentation

| | |
| --- | --- |
| [Overview](docs/index.md) | The system in one page, every feature and where it's documented |
| [Quickstart](docs/quickstart.md) | The whole system on your computer in containers, and a guided tour |
| [Getting started](docs/getting-started.md) | From nothing to a first recorded agent session |
| [Architecture](docs/architecture.md) | How it works inside |
| [Networking and protocols](docs/networking.md) | Every connection, port, message and routing rule |
| [Agents](docs/agents.md) | The `agent` command, every supported agent, what the requests are |
| [MCP servers](docs/mcp.md) | Sharing MCP servers from machines with agents |
| [Machines](docs/machines.md) | The fleet and `agent-machine` |
| [Providers](docs/providers.md) | Ollama, Strata, Docker Model Runner, containers, hosted APIs |
| [The console](docs/console.md) | Every page of the web console |
| [Accounts and sharing](docs/accounts.md) | Users, organizations, teams, sharing, phones |
| [OIDC authentication](docs/oidc.md) | The sign-in service, device sign-in, service accounts |
| [Security](docs/security.md) | Device approval, tokens, encryption in transit, a checklist for a public server |
| [Configuration](docs/configuration.md) | The database and stored settings, environment variables, server commands |
| [API](docs/api.md) | Every endpoint and WebSocket |
| [Deployment](docs/deployment.md) | Docker, HTTPS, installers, packages, updates |
| [Development](docs/development.md) | Building, testing, releasing |

## The parts

| Part | Folder | What it is |
| --- | --- | --- |
| Server | `server/` | `agent-server` (Python): the API, recording, routing, accounts; optionally serves the console |
| Console | `ui/` | The web console (React), bundled or [hosted separately](docs/deployment.md#separate-console-and-api-domains) |
| Public site | `site/` | The brochure website, separate from the application console |
| Client | `client/` | `agent` (Go): the launcher and each computer's `agent-proxy` |
| Machine | `machine/` | `agent-machine` (Go): keeps a computer in the server's fleet |
| Android app | `android/` | Your servers' consoles on your phone |
| Python client and machine | `python/` | `agent` and `agent-machine` for computers without a Go build |

See [Contributing](CONTRIBUTING.md) for changes and issue reports, [Development](docs/development.md) for builds and tests, and [`AGENTS.md`](AGENTS.md) for the code's structure and rules. Full traces can contain sensitive code and secrets; review [Security](docs/security.md) and configure recording retention before sharing a deployment.
