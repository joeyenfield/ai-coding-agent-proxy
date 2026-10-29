# agent and agent-machine in Python

The Python versions of the agent machine's two programs, for any computer with Python 3.9
or later. They work the same way as the Go ones (`../client`, `../machine`): they speak the
same server protocol, use the same files and ports, and pass the server's contract tests.
Use them where there's no Go build for the platform, or to read and change the programs
without a Go toolchain.

```bash
pip install https://server:9999/downloads/<version>/python/ai_proxy_agent_py-<version>-py3-none-any.whl
                                         # the server's own build: its Downloads page has the exact line
pip install ./python                     # or from a checkout: pip install -e "./python[dev]"
agent login http://server:9999           # sign this machine in, once (same file as the Go agent)
agent qwen . --model qwen3.6:35b         # launch an agent through this machine's agent-proxy
agent-machine login http://server:9999
agent-machine launches on && agent-machine roots add ~/work
agent-machine start                      # join the fleet in the background
```

The commands are `agent`, `agent-proxy` and `agent-machine`, the same as the Go programs', so
everything (docs, scripts, the console's commands) works the same whichever you install. Install
one or the other on a computer, not both: whichever comes first on PATH wins. In a virtualenv or
with `pipx` they stay out of the way. `python -m agent_py.client` and `python -m agent_py.machine`
work too.

It needs httpx, websockets, starlette and uvicorn (the server's own dependencies), and
pywinpty on Windows. psutil is optional (`[metrics]`) for CPU and memory figures.

## What's the same

**agent** (`agent_py/client`): `agent <agent> [project] [options] [-- args]`, `--list`,
`login`/`logout`, `proxy` and `proxy stop`, `version`, with the Go launcher's options
(`--model`, `--backend`, `--trace`, `--mcp`, `--control`, `--switch`, `--share`, `--team`,
`--let-others-type`, and `--headless` for launches from the console). The agent-proxy is the
same: loopback only, Host and Origin checked on every route, only the launcher's short list
of calls signed with the machine's token, agents' traffic streamed without it, terminals
relayed, and account agents' HTTPS tunnelled to the server's intercept over a WebSocket each.
`--control` runs the agent in a POSIX pty from the standard library, or a ConPTY through
pywinpty on Windows.

**agent-machine** (`agent_py/machine`): `login`/`logout`, `run`/`start`/`stop`, the status
display, `launches`, `roots`, `commands`, `name`, `address`, `servers` (and `servers service`),
`containers` and `mcp`. In the fleet it sends
the same hello and updates, runs launches (checking launches are on and the folder is in a root,
after resolving links), lists the folders inside its roots for the console's folder picker, opens
terminals with `commands on` (`agent --shell`, which the Python agent has too), carries tunnels to
its model servers, shares MCP servers through the same bridge as the Go one (`agent_py/machine/mcp.py`,
the same `mcp.json`), and answers the server's questions
about them: Ollama's, Docker Model Runner's and Strata's status, models and logs (pull
streamed; Strata's key read from its profile), searches of ollama.com, Hugging Face and Docker
Hub, and the machine's CPU, memory and NVIDIA GPUs. It runs containers for the server (Docker
or Podman, `agent_py/machine/containers.py`), and with `servers on` starts and stops Ollama (or
its systemd `ollama.service`, after `sudo agent-machine servers service`) and Strata's profiles.

## Same as Go, feature for feature

Both programs do everything their Go versions do; the Go code is the reference, and a feature
added there is added here in the same change. What differs is only how: pip instead of binaries
for updates, and the agent and agent-machine sharing one package (so `update --with-agent`
without `AI_PROXY_AGENT` stops the agent-proxy rather than running a second update). Installed
with pip, `sudo agent-machine servers service` may need the full path:
`sudo "$(which agent-machine)" servers service`.

The two programs live in one package and share `common.py` (the protocol, headers, sign-in
files, HTTP clients) and `login.py`. Each still keeps its own credentials, in the same place as
its Go version: `agent-proxy/credentials.json` (or `AI_PROXY_CREDENTIALS`) for agent, the
`agent-machine` folder (or `AI_PROXY_MACHINE_HOME`) for agent-machine.

A launch from the console runs `AI_PROXY_AGENT` if it's set, otherwise the Python agent with
the same Python as agent-machine.

## Updating and versions

`agent-machine update` and `agent update` install the build the server offers: its wheel from
`/downloads/`, checked against the server's SHA-256, installed with pip into the Python that runs
them. agent-machine then restarts itself if it runs in the background (`agent-machine start`), or
leaves it to its service; agent stops the running agent-proxy, so the next `agent` starts the new
one. That's what the console's **Update available** note asks for. With `agent-machine updates on`,
the console's **Update now** does it from there (`agent-machine update --with-agent`, detached).

The version the programs report, and the console compares with the server's:

- From the server's wheel: that build's version (tools/dist stamps `agent_py/_version.py`).
- Run from a checkout (`python -m agent_py.machine` inside `python/`, or with it on PYTHONPATH):
  the checkout's version, worked out from git as the server does, so a checkout at the server's
  commit matches it.
- Installed from a checkout with `pip install ./python`: `pyproject.toml`'s X.Y.Z only, which
  matches no build. Run `agent-machine update` to get the server's build instead.

Python runs a package from the folder it's started in before the installed one, so an
agent-machine started inside `python/` runs the checkout, whatever pip installed.

## Building it

`scripts/build-dist.sh` (`.\scripts\build-dist.ps1`) builds the wheel with everything else,
into `dist/<version>/python/`, listed in that version's SHA256SUMS, so the server offers it on
its Downloads page. `tools/dist/python.go` writes it in Go from `pyproject.toml` and
`agent_py/`, so no Python build tools are needed and the wheel is the same from any build
machine. It adds `agent_py/_version.py` with the build's version, so the installed programs
report the same version as the server's build (a checkout says `X.Y.Z+python`).
`PYTHON=0` (`-NoPython`) leaves it out. A `-version` that isn't a Python version (X.Y.Z, or
X.Y.Z+local) skips it with a note.

## Tests

```bash
cd python && python -m pytest -q
```

`tests/test_contract.py` runs the server's contract tests from `server/tests/test_client.py`
against these programs instead of the Go builds (it needs the server package installed), plus
versions of the model-server and tunnel tests that don't need containers.
