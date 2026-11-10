# Run it on your own computer

`agent-server local` runs a whole server on your computer, for you alone: the API and console, and the sign-in service (`agent-auth`). It needs no containers, no database server and no HTTPS. Everything stays on loopback and in one folder of yours.

Use it to try the system with your own folders and agents. For a demo with sample machines, see the [Quickstart with Docker or Podman](quickstart.md). For a server other people use, see [Getting started](getting-started.md).

## Install from the web

You need Python 3.11 or later, nothing else. On Linux, macOS or FreeBSD:

```bash
curl -fsSL https://github.com/joeyenfield/ai-control-mesh/releases/latest/download/install-local.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://github.com/joeyenfield/ai-control-mesh/releases/latest/download/install-local.ps1 | iex
```

The installer comes with each [release](https://github.com/joeyenfield/ai-control-mesh/releases). It does all of this:

1. It downloads the server, with its console already built, and checks its SHA-256.
2. It installs the server into a Python environment of its own (`venv` in the folder below) and starts it with `agent-server local start`.
3. It downloads `agent` and `agent-machine` for your computer (the Python versions where there's no Go build), checks them, and installs them into `~/.local/bin` (Windows: `%LOCALAPPDATA%\Programs\agent-proxy`, added to your PATH). `agent-server` goes there too.
4. It signs `agent` and `agent-machine` in to the new server, with launches and files on, and starts the machine. Each sign-in prints a link and a code: open the link, sign in as `admin` with the password it printed, enter the code and approve.

Then add a folder for the console to start agents in: `agent-machine roots add ~/projects`.

Run it again to update: it stops the server, replaces its files and starts it again. Your data stays. When it's done it says again where the console is and where the `admin` password is kept. [Remove it](#remove-it) has its twin, which takes it all out.

| Option (sh, PowerShell) | What it does |
| --- | --- |
| `--port N`, `-Port N` and `--auth-port N`, `-AuthPort N` | Other ports for the console and `agent-auth` (default 19999 and 20000). The choice is kept. |
| `--prefix DIR`, `-Prefix DIR` | Where `agent-server`, `agent` and `agent-machine` go. The sh installer's default is `~/.local/bin`, or `/usr/local/bin` when run as root. |
| `--no-client`, `-NoClient` | Only the server: no `agent` or `agent-machine` |
| `--no-login`, `-NoLogin` | Don't sign them in; print the commands instead |
| `--python`, `-Python` | The Python `agent` and `agent-machine` rather than the Go builds. They go into the server's Python environment. It's used anyway on a system without a Go build. |
| `--no-tray`, `-NoTray` | No [tray icon](#the-tray-icon). Without a desktop (a Linux server over SSH, say) it's left out by itself. |

Pass options like this: `curl -fsSL <url>/install-local.sh | sh -s -- --port 29999`, or in PowerShell `& ([scriptblock]::Create((irm <url>/install-local.ps1))) -Port 29999`.

The installer only trusts the SHA-256 of each download that was written into it when the release was made. On Debian and Ubuntu, Python's environments need `sudo apt install python3-venv`.

## Try it in one container

With Docker or Podman, one container holds the whole system: the server, sign-in and console, and a machine of its own fleet with the coding agents installed and Ollama to start from the console.

```bash
docker run -d --name mesh -p 127.0.0.1:19999:19999 -p 127.0.0.1:20000:20000 \
  -v mesh-data:/data -v mesh-home:/home/agent -v mesh-workspace:/workspace \
  ghcr.io/joeyenfield/ai-control-mesh
docker logs mesh
```

`docker logs` prints the console's address and the `admin` password. Open `http://localhost:19999` and sign in. The container is already a machine on the **Machines** page, signed in as `admin`, with launches, files, terminals and model servers on for `/workspace`. Start Ollama there, pull a model and start an agent, as in the [quickstart's tour](quickstart.md).

- **The ports must stay 19999 and 20000 on your side too.** The browser and the server inside both sign in at `http://localhost:20000`.
- **Your data lives in the three volumes:** `/data` (the server), `/home/agent` (the machine's sign-ins and the agents' logins) and `/workspace` (the projects). `docker rm -f mesh` keeps them; `docker volume rm mesh-data mesh-home mesh-workspace` deletes them.
- **`-e MESH_ADMIN_PASSWORD=...`** sets `admin`'s password yourself, from the environment, instead of a made-up one.
- **Tools:** it's Ubuntu with the sandbox's everyday tools (git, gh, ssh and sftp, ping, dig, build tools, Python; [the list](machines.md#a-sandbox-machine-in-docker)). `-e AGENT_APT_PACKAGES="nmap postgresql-client"` adds packages on start, and agents can `sudo apt-get install` more.
- **Your own computer can join too.** `agent login http://localhost:19999` and `agent-machine login http://localhost:19999` sign it in like any machine.

It's for trying the system. For a server other people use, see [Getting started](getting-started.md).

## Install from a checkout

To run the code in a checkout (your own changes, say), you need Python 3.11 or later. With Node.js (npm) the console is built for you; with Go, `agent` and `agent-machine` are built from the checkout, else the Python versions are installed.

```bash
git clone <this repository> ai-control-mesh
cd ai-control-mesh
./devhelpers/scripts/install-local.sh          # Windows: .\devhelpers\scripts\install-local.ps1
```

The script installs the server, builds what it can and runs `agent-server local start`. It stops with a message when there's no Python 3.11 or later. Where the system's Python doesn't take pip installs (Debian, Ubuntu, Homebrew), it makes an environment in the checkout's `.venv` and installs there; an environment you activated yourself is used as it is.

## The first start

When the server is ready, either installer prints:

- the console's address, `http://localhost:19999`;
- the password of the first account, `admin`;
- the commands that connect this computer to it.

Open the console and sign in as `admin`. Change the password in **Organizations › Everyone › Edit user**, then delete the `first-user-password` file it names.

## Connect your computer

The web installer has done this already, except `roots add`. From a checkout, run agents through it and let the console start agents in your folders:

```bash
agent login http://localhost:19999
agent-machine login http://localhost:19999 --enable launches,files
agent-machine roots add ~/projects
agent-machine start
```

Each `login` prints a link and a code: open the link, sign in, enter the code and approve. Then `agent qwen .` (or any agent from `agent --list`) runs an agent through your local server, and the machine shows on the console's **Machines** page. See [Agents](agents.md) and [Machines](machines.md).

`localhost` is the one name it signs in on. Use `http://localhost:19999`, not `127.0.0.1`, in the browser and the commands.

## Start, stop and status

| Command | What it does |
| --- | --- |
| `agent-server local` or `agent-server local start` | Starts the server and `agent-auth` in the background, waits until both answer, and prints the next steps. Does nothing if they're running. |
| `agent-server local stop` | Stops the two processes it started. It never stops another program, even one on the same port. |
| `agent-server local status` | Shows the folder, the address and whether each process runs. Exits 3 when they don't. `--json` prints it as one line of JSON, with the server's public address (`proxy.public_url`) when it has one: what the tray icon reads. |
| `--port`, `--auth-port` | Other ports for the API and console (default 19999) and for `agent-auth` (default 20000). The choice is kept. |

Start refuses a port another program uses: stop that program or pick another port. The quickstart uses the same ports, so stop one before starting the other.

It doesn't start again when you sign in to your computer. Run `agent-server local` after a restart, or use **Start the server** on the tray icon.

## The tray icon

On a desktop the installers put in `agent-tray`, an icon in the system tray. It's green when everything here runs, amber when some of it does, and grey when nothing does. Its menu shows, for whatever is on this computer:

| Part | What it shows | What it does |
| --- | --- | --- |
| The server | Running or stopped, and its address | **Open the console** (also a double-click), **Open** the server's public address when it has one (**Public console address** on **Server settings**, `proxy.public_url`), **Start the server**, **Stop the server** |
| This machine | Whether it's signed in and in the fleet, and as whom | **Start agent-machine**, **Stop agent-machine**, **Features** (each one ticked when it's on; click to switch it), **Folders** (its launch folders) |
| agent | Whether it's signed in, as whom, and whether `agent-proxy` runs | **Start agent-proxy**, **Stop agent-proxy** |

**Start at sign-in** is ticked by the installers. On Windows it's an entry in your sign-in programs; on macOS a LaunchAgent; on Linux an XDG autostart file.

- **Windows:** new icons go behind **^** in the taskbar. Drag it out to keep it in view.
- **Linux:** the icon needs a system tray. KDE, XFCE and most desktops have one. GNOME needs the AppIndicator extension, which Ubuntu has by default.
- **What it runs:** the commands you'd type (`agent-server local`, `agent-machine`, `agent proxy`). It reads their `status --json` every few seconds, so it works with the Go programs and the Python ones alike. When one of them is too old to answer, the menu says so: update it.

| Command | What it does |
| --- | --- |
| `agent-tray` | Shows the icon. One runs at a time. |
| `agent-tray --autostart on\|off` | Starts it when you sign in, or not |
| `agent-tray --status` | Prints what the icon would show, as JSON |
| `agent-tray --check` | Exits 0 when an icon can be shown here |

On another computer with Python 3.9 or later, the server's own installer adds it ([Deployment](deployment.md#the-one-line-installers)). Elsewhere, `pip install "ai-proxy-agent-py[tray]"` from the wheel on the server's **Downloads** page gives you `agent-tray`.

## Where it keeps things

One folder holds everything: `AI_PROXY_LOCAL_HOME` if you set it, else

| System | Folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\AI Control Mesh` |
| macOS | `~/Library/Application Support/AI Control Mesh` |
| Linux | `$XDG_DATA_HOME/ai-control-mesh`, else `~/.local/share/ai-control-mesh` |

| File | What |
| --- | --- |
| `logs/agent-server.sqlite3` | The server's database: settings, accounts, machines, recordings |
| `oidc/issuer-state.sqlite3` | The sign-in service's tokens and signing key |
| `secrets.json` | The secret the two processes share and the vault key for stored provider keys, made on first start. Readable only by you. |
| `private/first-user-password` | The first account's password, until you delete it |
| `local.json` | The ports and the processes it started |
| `app/<version>/`, `venv/` | The server's files and its Python environment (with `agent-tray`), from the web installer |
| `logs/server.out.log`, `logs/auth.out.log` | Each process's output |

To start again from nothing, stop it and delete the folder. To back it up, stop it and copy the folder: `config export` alone isn't a backup.

## Remove it

The web installer's twin takes out what it put in: the tray icon and its start at sign-in, `agent` and `agent-machine` (with `agent-machine`'s service), the server's programs and the `agent-server` link.

```bash
curl -fsSL https://github.com/joeyenfield/ai-control-mesh/releases/latest/download/uninstall-local.sh | sh
```

```powershell
irm https://github.com/joeyenfield/ai-control-mesh/releases/latest/download/uninstall-local.ps1 | iex
```

| Option (sh / PowerShell) | What it does |
| --- | --- |
| `--purge` / `-Purge` | Also delete the server's data (its database, recordings and keys), sign `agent` and `agent-machine` out and delete their settings |
| `--keep-client` / `-KeepClient` | Leave `agent` and `agent-machine`, and their sign-ins, as they are |
| `--prefix DIR` / `-Prefix` | The folder the programs went to, if you chose one when installing |

Without `--purge` the data stays in the folder above, so installing again finds your settings and recordings. Pass options like the installer's: `curl -fsSL <url>/uninstall-local.sh | sh -s -- --purge`, or in PowerShell `& ([scriptblock]::Create((irm <url>/uninstall-local.ps1))) -Purge`.

An install from a checkout has no uninstaller: run `agent-server local stop`, `agent-tray --autostart off` if you use the tray, `pip uninstall ai-coding-agent-server`, and delete the folder.

The local server ignores `AI_PROXY_*` variables set in your shell, so one set up for another server can't point it elsewhere. Settings you change in the console are kept in its database as usual.
