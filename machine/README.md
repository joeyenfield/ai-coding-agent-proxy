# agent-machine (a machine in the fleet, Go)

The server is the hub, agents connect to it, and **machines** supply the LLM providers.
`agent-machine` is what makes a computer one of those machines: one binary, with nothing
else to install. While it runs, the server can use the model servers this machine runs,
run Strata or Ollama here in containers, and start agents here, once the machine allows
each.

```bash
agent-machine login https://your-server:9999   # once: its own sign-in, apart from `agent login`
agent-machine servers on                       # optional: let the console manage the model servers here (start, stop, settings, models)
agent-machine containers on                    # optional: let the server run containers here (Docker or Podman)
agent-machine start                            # join the fleet, in the background
agent-machine                                  # how the fleet sees this machine
```

| Command | What it does |
| --- | --- |
| `agent-machine` | This machine: its id, server and sign-in, whether it's in the fleet, the model servers found, containers and launches |
| `agent-machine login [url]` | Sign in with an OIDC device code approved in your browser |
| `agent-machine logout [url]` | Forget the sign-in and revoke its token |
| `agent-machine start` / `stop` | Join the fleet in the background, or leave it |
| `agent-machine run [--server url]` | Stay in the fleet in the foreground (Ctrl+C leaves) |
| `agent-machine containers on\|off` | Let the server run Strata or Ollama here in containers (off by default) |
| `agent-machine containers cli auto\|docker\|podman` | Which container CLI runs them: by default (auto) Docker when it's installed and answering, else Podman |
| `agent-machine servers on\|off` | Let the console manage the model servers here: start and stop the Ollama and Strata installed here, change Ollama's settings, and pull, delete, load and unload models (off by default) |
| `sudo agent-machine servers service [off]` | Linux: let agent-machine start, stop and restart an Ollama that's the systemd service `ollama.service`, with the console's settings (a sudoers rule for only those three commands, and a drop-in that reads `/etc/agent-machine/ollama.env`) |
| `agent-machine launches on\|off`, `agent-machine roots add\|remove <dir>` | Let the server start agents here, in those folders only (off by default) |
| `agent-machine updates on\|off` | Let the console update agent-machine (and agent, if asked) here to the server's build, with **Update now** on the machine's page (off by default) |
| `agent-machine commands on\|off` | Let the owner open a terminal here from the console: a shell in one of the roots, as this user, that only they see (off by default) |
| `agent-machine name <name>\|-`, `agent-machine address <host>\|-` | The name the console shows, and how the server reaches this machine's model servers |
| `agent-machine update [--force]` | Replace agent-machine with the build the server offers for this computer, and restart it |
| `agent-machine version` | Its version and the server protocol it speaks |

## How it works

Sign-in is mandatory, including on loopback. URLs require HTTPS except loopback development.
OAuth access and rotating refresh credentials are kept in the machine's own
`credentials.json`, separate from `agent login`; concurrent refresh is serialized.
Logout revokes the refresh credential and removes the local login, reporting revocation
failures. Password and access-key sign-ins are not supported. For automation obtain a
service-account OAuth access token directly from the issuer and set
`AI_PROXY_MACHINE_TOKEN`; use `run --server <url>` to select its API. `AI_PROXY_TOKEN`
remains the agent command's credential and is never used for the fleet connection.

`agent-machine run` (which `start` runs detached) keeps one WebSocket open to the server
(`/api/machines/connect`), signed with its token, and reconnects when it drops. Over it:

- **Hello and updates, every minute:** the machine's id, name and OS, the model servers
  answering on their default ports on loopback (Ollama 11434, Docker Model Runner 12434,
  Strata 18500), the server's agents installed here, whether it runs containers (its
  CLI, whether the engine answers, the engines it offers) and its launch settings. The
  console adds a model server as a provider from this ("Add as a provider").
- **MCP servers** (`agent-machine mcp add`, in `mcp.json` here): the hello names them, with
  the loopback port and per-run key of the bridge (`internal/mcp`) that serves them as Streamable
  HTTP. The server reaches it through a tunnel like a model server's. A stdio server gets a process
  for each MCP session; a URL server is proxied with the headers set here. Their commands,
  environments and headers never leave this machine.
- **Updates** (`{"type": "update", "agent": ...}`, with `updates on`, answered by `update_result`):
  runs `agent-machine update --server <its server>` detached (with `--with-agent`, `agent update`
  first), so it can stop this process and start the new binary; its output goes to the log. It only
  installs the server's own build, checked against its SHA-256, like `agent-machine update`.
- **Folders** (`{"type": "folders", "path": ...}`, answered by `folders_result`): the folders
  inside one of the roots, for the console's folder picker. Only while launches or commands are on,
  only inside the roots (checked here after resolving links), only folder names, and no hidden
  folders or links out of the roots.
- **Terminals** (`{"type": "shell", "project": ...}`, with `commands on`): runs
  `agent --shell <folder> --headless`, the user's shell in a terminal shared with the server for the
  owner only, answered like a launch (`launched` with its session).
- **Container requests** (`{"type": "container", "op": ...}`): status, start, stop,
  restart, remove, logs. The machine runs `docker` (or `podman`) itself, with its own
  environment, so `DOCKER_HOST` and docker contexts apply. `docker run` is root here, so
  every request is checked whatever the server allowed: only its own engine table
  (`internal/containers`), each engine's own settings, plain image names, GPU lists, a
  volume name or absolute folder, and listening on 127.0.0.1 or 0.0.0.0.
- **Questions about its model servers** (`{"type": "provider", "kind": "ollama"|"strata"|"docker", "port": ..., "op": ...}`,
  answered by `provider_result`; package `internal/providers`). The server never talks
  to a model server to manage it: it asks the machine that runs it. Ops: `status` (models,
  what's loaded, Strata's health and key, Docker's engines and disk), `pull` (streamed
  as `provider_progress` messages), `delete`, `load`, `unload`, `log` (Strata's server or
  engine log with its key blanked out; the Ollama agent-machine started), `metrics` (this
  machine's CPU, memory and NVIDIA GPUs) and `search` (ollama.com, Hugging Face GGUF repos
  and Docker Hub, searched from here). Requests name a kind, a port and a model only, and
  each op calls fixed paths on `127.0.0.1:<port>`. Reading is always allowed; changing
  models needs `servers on`. Strata providers in the hello carry their profile's API key,
  read from Strata's own files, so the server can forward agents' requests to it; the
  server keeps it in memory only.
- **Model servers** (`{"type": "server", "kind": "ollama"|"strata", "op": "start"|"stop"|"restart"|"settings"|"configure"}`),
  with `servers on`. Ollama is started as `ollama serve` (from `PATH`, or where its
  installer puts it), listening on 127.0.0.1 when the server is this computer and on
  every address otherwise, with its log in `ollama.log`. It starts with the OLLAMA_*
  settings saved from the console (`ollama.json` here; `settings` reads them with their
  table, `configure` saves them); extra variables can only be `OLLAMA_*` ones, and
  `OLLAMA_HOST` is always agent-machine's. agent-machine only stops an
  Ollama it started; one run by its tray app or a service is shown as running and left
  alone. Strata runs one of this machine's saved profiles through Strata's own
  `strata runtime start|stop --profile <id>` (from `PATH`, or the terminal Strata's
  desktop app installs), so Strata owns the instance and drains it before stopping.
  Requests can only name a kind and one of the machine's own profiles. With servers on,
  each model installed in Strata (`strata models list --installed`) that no profile uses
  gets one (`strata profile create`), and a profile whose run config is gone is reported
  as broken and not started.
- **Launches** (`{"type": "launch"}`): checked against `launches` and the roots (links
  resolved), then run as `agent <id> <folder> --headless --launch-id <id>`. They need the
  agent command on `PATH` (or `AI_PROXY_AGENT`), signed in to the same server with
  `agent login`: the agent goes through this computer's agent-proxy like any other.

**Versions.** Every request agent-machine sends to the server, the fleet socket
included, carries `X-AI-Proxy-Version`, `X-AI-Proxy-Protocol` (`settings.Protocol`) and a
`User-Agent` of `agent-machine/<version>`; the hello has `protocol` too. Model servers and
model catalogues get none of this: they're asked with the plain client. A server that
can't take its protocol closes the socket with `4426` (or answers `426`), and the reason
shows in the status and the log. Each time it connects it reads the server's version from
its public `/health`: when that isn't its own, the log and `agent-machine` say
`Update with: agent-machine update`.

**Updating.** `agent-machine update` reads the server's `/downloads/index.json`, picks
`<os>-<arch>/artifact/agent-machine` (`.exe` on Windows), downloads it beside the installed
binary, checks its SHA-256, and swaps it in: the old file becomes `agent-machine.old`
(kept on Windows until the next start, as a running program can't be deleted there;
removed at once elsewhere). A folder you can't write (installed by a package) fails with a
pointer to the package manager. Then the running agent-machine moves to the new binary,
depending on how it was started (`status.json` keeps it): one started with
`agent-machine start` is stopped and started again; one run by a service (systemd,
launchd: no terminal) is stopped and its service starts it again from the new file; one
running in a terminal is left for you to restart.

Its state lives in its own folder (`%APPDATA%\agent-machine`, `~/.config/agent-machine`,
or `AI_PROXY_MACHINE_HOME`): `credentials.json`, `machine.json` (the id the server knows
the machine by, and its settings), `status.json` (how a running agent-machine is doing)
and `agent-machine.log`. A `machine.json` from before agent-machine existed (beside
`agent login`'s credentials) is carried over once, so the machine keeps its id and the
providers that point at it.

Podman works like Docker, with one difference handled here: GPUs go to Podman as CDI
devices (`--device nvidia.com/gpu=all`, which needs the NVIDIA Container Toolkit's CDI
spec), where Docker takes `--gpus`. With Podman on Windows or macOS, its machine must be
running (`podman machine start`).

`AI_PROXY_CONTAINER_COMMAND`, `AI_PROXY_OLLAMA_COMMAND` and `AI_PROXY_STRATA_COMMAND`
(JSON lists of arguments) replace the container CLI, `ollama` and `strata`; the tests use
the server's fakes this way.

## Layout

```text
cmd/agent-machine/      the binary
internal/command/       the command line: login, run/start/stop, settings, the status display
internal/presence/      the fleet connection: hello and updates, container, server and provider requests, launches, model server detection
internal/containers/    containers the server asks for: engines, spec checks, docker/podman run, jobs, which CLI is installed
internal/servers/       starting and stopping the Ollama and Strata installed here, and Ollama's settings
internal/providers/     the model servers' state, models, logs; this machine's load; model catalogue search
internal/settings/      its folder, credentials, machine.json, status.json, HTTP clients without environment proxies
internal/login/         `agent-machine login` and `logout`
internal/update/        `agent-machine update`: the server's downloads index, checking, swapping the binary
internal/process/       running programs detached or without a window; whether a process is alive
```

## Building and testing

Go 1.24 or later. From the repository root, `scripts/build-machine.sh` (or
`.\scripts\build-machine.ps1`) builds `machine/bin/agent-machine` for this machine;
`scripts/build-dist.sh` builds it for every platform with the client. The archive's
`install.sh --service` (`install.ps1 -Service` on Windows) installs it and starts it at
boot or sign-in; see the main README's Installing on a machine section.

```bash
go build -o bin/agent-machine ./cmd/agent-machine
go test ./...
```

`../server/tests/test_client.py` builds it with the client and runs both against a real
agent-server: joining the fleet, a launch from the console, containers on the fake docker,
and a Strata profile started and stopped through the fake strata.
