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
(`--model`, `--backend`, `--trace`, `--control`, `--switch`, `--share`, `--team`,
`--let-others-type`, and `--headless` for launches from the console). The agent-proxy is the
same: loopback only, Host and Origin checked on every route, only the launcher's short list
of calls signed with the machine's token, agents' traffic streamed without it, terminals
relayed, and account agents' HTTPS tunnelled to the server's intercept over a WebSocket each.
`--control` runs the agent in a POSIX pty from the standard library, or a ConPTY through
pywinpty on Windows.

**agent-machine** (`agent_py/machine`): `login`/`logout`, `run`/`start`/`stop`, the status
display, `launches`, `roots`, `commands`, `name`, `address` and `servers`. In the fleet it sends
the same hello and updates, runs launches (checking launches are on and the folder is in a root,
after resolving links), lists the folders inside its roots for the console's folder picker, opens
terminals with `commands on` (`agent --shell`, which the Python agent has too), carries tunnels to
its model servers, and answers the server's questions
about them: Ollama's status, pull (streamed), delete, load, unload; Docker Model Runner's
status and models; Strata's status; and the machine's CPU, memory and NVIDIA GPUs.

## What's only in the Go versions

- Containers (`agent-machine containers ...`). The hello says containers are off, so the
  console doesn't offer them, and any container request is refused.
- Starting and stopping Ollama and Strata, and Ollama's settings (`servers` here only lets the
  server change models). The hello says so in the same way.
- Strata's load, unload and logs, and searching the model catalogues.
- `update`: upgrade with pip instead.

The two programs live in one package and share `common.py` (the protocol, headers, sign-in
files, HTTP clients) and `login.py`. Each still keeps its own credentials, in the same place as
its Go version: `agent-proxy/credentials.json` (or `AI_PROXY_CREDENTIALS`) for agent, the
`agent-machine` folder (or `AI_PROXY_MACHINE_HOME`) for agent-machine.

A launch from the console runs `AI_PROXY_AGENT` if it's set, otherwise the Python agent with
the same Python as agent-machine.

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
