# agent (the client, Go)

What runs on each machine where you start coding agents: one binary, with nothing else
to install. For a machine that supplies LLM providers to the server (its Ollama, Strata,
Docker, containers) or takes launches from the console, see `agent-machine` in
[`../machine`](../machine/README.md).

| Command | What it does |
| --- | --- |
| `agent <agent> [project] [options] [-- args]` | Launch an agent through this machine's agent-proxy, in a session on the server |
| `agent --list` | The server's agents, and whether each is installed here |
| `agent login [url] [--user name] [--key key]` | Sign this machine in to an agent-server (a token, never your password, is stored) |
| `agent logout [url]` | Forget the sign-in and revoke its token |
| `agent proxy` | Run this machine's agent-proxy in the foreground (`agent` starts it in the background when needed) |
| `agent proxy stop` | Stop the agent-proxy running here; the next `agent` starts it again |
| `agent update [--force]` | Replace `agent` and `agent-proxy` with the build the server offers for this computer, then stop the running agent-proxy |
| `agent version` | The client's version and the server protocol it speaks |

`agent-proxy` (in `cmd/agent-proxy`) is the same proxy as `agent proxy`, as a binary of
its own for running it as a service.

## How it works

The client only knows the server's URL (from `agent login`, `AI_PROXY_URL`, or
`http://127.0.0.1:9999`). The agent definitions, the backend for each agent and the
environment to launch it with all come from the server's API, through the agent-proxy:

1. `agent` starts the agent-proxy (`127.0.0.1:8184` and `:8183`) if it isn't running,
   and the `agent-server` too when the server is this computer and it's installed here.
2. It asks the server for the agent (`GET /api/agents`), finds its executable on `PATH`,
   and for account agents downloads the server's CA (`/api/intercept/ca.pem`, `bundle.pem`).
3. `POST /api/agents/<id>/sessions` opens the session and returns the environment and
   command, rendered for this machine's agent-proxy.
4. It runs the agent, in a pseudo-terminal shared with the server with `--control`
   (ConPTY on Windows, a pty elsewhere), then closes the session with its exit status.

The agent-proxy listens on loopback only and refuses web pages (it checks the Host and
Origin headers). It signs only a short list of launcher calls with the machine's token;
agents' own traffic is passed through with their session id as the credential. It
doesn't join the server's fleet: that's agent-machine's job.

**Versions.** Every request the client sends to the server says which client it is:
`X-AI-Proxy-Version`, `X-AI-Proxy-Protocol` (the server API level, `settings.Protocol`) and
a `User-Agent` of `agent/<version>`; the agent-proxy adds the first two to everything it
forwards, agents' own traffic included (agents keep their own User-Agent). A server that
can't serve that protocol answers `426` with what to do, and `agent` prints it and exits.
When the agent-proxy running here or the server is another version than `agent`, `agent`
says so on stderr (a build without a version, `dev`, doesn't). The agent-proxy's `/health`
has its `version` and `protocol`, and `POST /shutdown` (from this computer only, like the
rest) stops it: that's `agent proxy stop`.

**Updating.** `agent update` reads the server's public `/downloads/index.json`, picks
`<os>-<arch>/artifact/agent` and `agent-proxy` (`.exe` on Windows; 32-bit ARM is
`armv7`), downloads each beside the installed binary and checks its SHA-256 against the
index; nothing is replaced unless both check out. Then each is swapped in: the old file is
renamed to `<name>.old` and the new one renamed into place. On Windows the `.old` file is
kept, as a running program can't be deleted there, and removed the next time `agent` or
`agent-proxy` starts; elsewhere it's removed at once. A folder you can't write (a package's
`/usr/bin` or `Program Files`) fails with a pointer to the package manager. Finally it
stops the running agent-proxy, which is the old binary, so the next `agent` starts the new
one. It updates from the server the agent-proxy carries traffic to (`--server` for
another), and does nothing when that's already this version (`--force` anyway).

`agent <agent> <folder> --headless --launch-id <id>` is how agent-machine starts an agent
the console asked for: it prints `{"session_id": ...}` as its only line of output, then
runs the agent with no local window, its terminal shared with the server. The session
is tagged with the fleet machine from `AI_PROXY_MACHINE_ID` and `AI_PROXY_MACHINE_NAME`.

## Layout

```text
cmd/agent/              the agent binary: subcommands
cmd/agent-proxy/        the agent-proxy binary
internal/settings/      ports, `agent login`'s credentials file, HTTP clients without environment proxies
internal/api/           JSON calls to the server through the agent-proxy
internal/localproxy/    the agent-proxy: launcher API allowlist, agent traffic, terminal relay, HTTPS tunnel
internal/launcher/      `agent`: arguments, the launch, starting the agent-proxy and server, containers
internal/login/         `agent login` and `agent logout`
internal/terminal/      --control: history, the share connection, ConPTY and pty hosts
internal/process/       starting programs detached from the terminal
internal/update/        `agent update`: the server's downloads index, checking, swapping binaries
```

## Building and testing

Go 1.24 or later. From the repository root, `scripts/build-client.sh` (or
`.\scripts\build-client.ps1`) builds `client/bin/agent` for this machine.
`scripts/build-dist.sh` (`.\scripts\build-dist.ps1`, or `--all`/`-All` here) builds it,
and agent-machine, for every standard platform into `dist/<version>/<os>-<arch>/`, each archive with
an installer (`install.sh` or `install.ps1`), and with `PACKAGES=1` native packages too;
see the main README's Distributions and Installing on a machine sections.

```bash
go build -o bin/agent ./cmd/agent
go test ./...
```

`../server/tests/test_client.py` builds this client and runs it against a real
agent-server, so changes to the contract between the two are tested from both sides.
