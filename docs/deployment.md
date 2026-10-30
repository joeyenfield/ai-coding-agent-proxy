# Deployment

**Authentication is mandatory.** For public deployments, run `agent-auth` separately at **auth.aicontrolmesh.com**, register API callbacks, configure the shared introspection secret and provide HTTPS. The root Compose includes the issuer on host loopback :10000 with private persistent state. See [OIDC authentication](oidc.md) before starting or upgrading public services. The local demo below includes its own loopback issuer.

How to run the server for other machines and people, put HTTPS in front of it, and get `agent` and `agent-machine` onto the computers that use it.

## Local container quickstart

For a local demo, the checkout's `quickstart/` folder contains a separate Compose project
with the API and console, its OIDC issuer, two fleet machines, and Ollama on machine-1.
From the repository root:

```bash
podman compose -f quickstart/compose.yaml up -d --build
```

Use `docker compose` instead for Docker. With Podman on Windows/macOS, start the VM
with `podman machine start` first (`podman machine init` once if none exists).
`podman compose version` checks the Compose provider; Podman Desktop can install it
under **Settings > Resources > Compose**. A provider named Docker Compose works with
Podman: `podman compose` connects it to the Podman engine.

Open **http://localhost:19999**, using `localhost` exactly, and sign in with **admin** /
**secret**. Sign-in uses the local issuer on `http://localhost:20000`. Both published ports
bind host loopback. The demo's environment-defined admin password can be overridden with
`QUICKSTART_ADMIN_PASSWORD`; these known demo credentials are for local use.

The machines automatically sign in separately for the launcher and fleet connection.
They have independent home/workspace volumes and allow launches and shells in `/workspace`.
Ollama runs in its own container on CPU by default; machine-1 relays its loopback port
11434 to it, where agent-machine finds it. No host project folders or Docker socket are mounted.

In **Machines**, open **machine-1** and choose **Add as a provider** for Ollama. This creates
the default `machine-1` backend. Pull a model from its model page, then start an agent on
either machine with that backend and model. No model is downloaded automatically.
The server reaches Ollama through the fleet tunnel; model operations run on machine-1.
Compose controls the Ollama process itself.

Use `podman compose -f quickstart/compose.yaml logs -f machine-1 machine-2` to inspect the
machines and `podman compose -f quickstart/compose.yaml stop` to stop the demo. Volumes
preserve configuration, auth state, recordings, workspaces and models. To delete all of
that demo state, run `podman compose -f quickstart/compose.yaml down -v`. Reset or sign the
machines in again after changing the admin password, which invalidates their tokens.

The helper scripts in `quickstart/` forward the canonical localhost addresses inside
containers and provision machine credentials through normal issuer device authorization.
They do not change the server's authentication rules. For public deployment use the
HTTPS setup below and the [OIDC deployment guide](oidc.md).

Images use explicit registries to avoid Podman's short-name selection prompt. The
shared read-only helper mounts use SELinux's `z` label. Keep the services in separate
network namespaces: a container sharing another's (`network_mode: service:...`) stops
Podman from recreating that one after a rebuild.

The root `.dockerignore` excludes generated screenshot/test folders, virtual environments
and agent history. Keep these exclusions when customizing the build context: private
Windows ACLs on generated directories can otherwise cause `checking context: can't stat`
before a build starts.

On Windows, a `netavark`/`nftables` failure before containers start can be a WSL kernel
compatibility problem ([reported upstream](https://github.com/containers/netavark/issues/1495)).
Update WSL with `wsl --update`, save other WSL work, and restart WSL/Podman before
retrying. `wsl --shutdown` stops all distributions, so schedule it accordingly. The
quickstart retains bridge networking and does not disable the VM firewall.

## The server in Docker

`server/Dockerfile` builds one image with the server and its console. Compose also builds the static brochure site in `site/`. Build them from the repository root:

```bash
docker compose up -d                                  # with compose.yaml at the root
docker build -f server/Dockerfile -t agent-server .   # server and console, or by hand
```

The image builds the console with Node, then installs the server on `python:3.13-slim`. It runs as a non-root user (`agent`, uid 10001), listens on `0.0.0.0:9999`, and checks `/health` every 30 seconds. It sets `AI_PROXY_HOME=/app`, `AI_PROXY_HOST=0.0.0.0` and `AI_PROXY_UI_BUILD=0`.

The `marketing-site` service serves `site/` on `127.0.0.1:8080` for the reverse proxy. The public website uses the apex and `www` hostnames, the application console uses `console.<domain>`, and agent and machine clients can use `api.<domain>`.

| Volume | What it holds |
|---|---|
| `/app/config` | `backends.yaml`, `models.yaml`, `agents.yaml`. On the first start the entrypoint copies in the image's defaults, never overwriting what's there. |
| `/app/logs` | Sessions, traces, terminal history, the proxy log, `machines.json`. |
| `/app/certs` | The capture CA, `users.json`, `container-keys.json`, `oidc-browser-key` and `first-user-password`. |
| `/app/dist` | The builds to hand out (mount `dist/` read-only; `compose.yaml` does). |

The default `backends.yaml` in the image (`server/docker/backends.yaml`) has an Ollama backend at `http://host.docker.internal:11434` and the hosted Anthropic API. Inside the container `127.0.0.1` is the container itself, so `compose.yaml` maps `host.docker.internal` to the Docker host on Linux too. Local providers normally come from machines in the fleet, so in practice you run `agent-machine` where the models are and add them from the console ([Machines](machines.md)).

Authentication is always required. The first start creates an `admin` user with a random password if no accounts file exists. `docker compose logs agent-server` shows it, and it's saved as `first-user-password` in the certs volume. See [Accounts](accounts.md).

**Versions in Docker.** There's no git inside the image, so pass the version as a build argument: `AI_PROXY_VERSION=1.2.0+abc1234 docker compose build`. The server hands out the `dist/<version>/` of its own version, or the newest of the same `X.Y.Z` when that wasn't built, so match it to the builds you mount.

**A sandbox machine.** `docker compose --profile sandbox up -d` also starts `agent-sandbox` (`sandbox/Dockerfile`): `agent`, `agent-machine` and npm coding agents in a container that joins the fleet and takes launches in `/workspace` only. Set `AI_PROXY_SERVER` to your HTTPS API and optionally `AI_PROXY_MACHINE_NAME`. Approve both device codes from its logs on first start; persistent credentials refresh on later starts. Password/key bootstrap is removed. See [Machines](machines.md).

Podman builds and runs the same image; pass `--http-proxy=false` to `podman build`.

## HTTPS

Put HTTPS in front of the server whenever it's reachable from outside your own network. Without it, sign-in cookies, tokens and recorded traffic cross the network in the clear. What any reverse proxy in front needs to do (WebSockets, no buffering, long timeouts, `Host` and `X-Forwarded-For`) is in [Networking](networking.md#behind-a-reverse-proxy).

### Separate console and API domains

The console can run as static files, independently of `agent-server`:

| Hostname | Service |
|---|---|
| `aicontrolmesh.com`, `www.aicontrolmesh.com` | Brochure (`marketing-site`, loopback port 8080). |
| `console.aicontrolmesh.com` | Static console (`console`, loopback port 8081). No API forwarding. |
| `api.aicontrolmesh.com` | `agent-server` (port 9999): API, WebSockets, live streams, documentation files, downloads and installers. |
| `auth.aicontrolmesh.com` | `agent-auth` (loopback port 10000): OIDC sign-in, device approval and OAuth endpoints. |

Set these variables for Compose (or put them in the root `.env`, which is ignored by git), then run `docker compose --profile console up -d --build`:

```dotenv
VITE_API_URL=https://api.aicontrolmesh.com
AI_PROXY_SERVE_UI=0
AI_PROXY_CONSOLE_ORIGINS=https://console.aicontrolmesh.com
```

`VITE_API_URL` is a **build-time** setting. Rebuild the console when its API address changes. `ui/Dockerfile` builds it; `ui/nginx.conf` serves its assets and SPA routes, returns 404 for API paths and never forwards requests to the server. With no `VITE_API_URL`, a normal `npm run build` still uses the page's origin for bundled/local installs.

On the API server, `AI_PROXY_SERVE_UI=0` disables console routes, static assets and automatic UI rebuilding. `AI_PROXY_CONSOLE_ORIGINS` is a comma-separated list of exact trusted origins, including the scheme and any port, without paths or wildcards. The same list controls credentialed CORS, mutation origin checks and WebSocket origin checks. Leave it empty for same-origin-only installs.

Keep the console and API on HTTPS subdomains of the **same site**. Sign-in sets an HttpOnly, host-only cookie on the API domain; the console includes credentials on requests and live streams, and browsers send that cookie on API WebSockets. The cookie is not shared with the brochure or console hosts. Unrelated registrable domains are not supported by this SameSite=Lax cookie setup. Moving an existing console to the API host's cookie requires signing in again.

Set `proxy.public_url` to the console's HTTPS address. CLI installation/login uses `https://api.aicontrolmesh.com`; OIDC uses `https://auth.aicontrolmesh.com`. Include all hostnames in DNS and TLS. With Cloudflare, use Full (strict), and never cache API or auth responses.

### nginx and Let's Encrypt on Windows

`scripts/https/setup-https.ps1` sets up nginx with a Let's Encrypt certificate that renews itself. It serves the brochure site on the apex and `www` hostnames, the application on `console.<domain>`, and agent/machine API requests on `api.<domain>`. Point all four DNS names to this host, start the Compose services, then run it once from an elevated PowerShell; rerun it any time, and it picks up where things are:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\https\setup-https.ps1 -HostName aicontrolmesh.com -AdditionalHostNames aiproxy.hopto.me -Email you@example.net
```

It installs, under `-Root` (default `C:\ProgramData\agent-proxy-https`):

- **nginx** for Windows, serving plain HTTP on `-HttpPort` (9980) for Let's Encrypt's check, and HTTPS on `-HttpsPorts` (9443 and 9998). The apex and `www` proxy to `-SiteBackend` (`http://127.0.0.1:8080`), the console to `-ConsoleBackend` (`http://127.0.0.1:8081`), and the API to `-Backend` (`http://127.0.0.1:9999`);
- **win-acme**, the Let's Encrypt client, which proves the name with a file nginx serves under `/.well-known/acme-challenge/`, writes the certificate, and renews it from its own scheduled task, reloading nginx;
- a scheduled task, `agent-proxy-https nginx`, that starts nginx at boot, and firewall rules for the ports.

The name has to reach this computer from the internet on port 80 for Let's Encrypt (forward the router's 80 to `-HttpPort`), and on 443 for people (to the first of `-HttpsPorts`).

| Option | What it does |
|---|---|
| `-HostName` | The primary DNS name to get a certificate for (default: `aicontrolmesh.com`). |
| `-ConsoleHostName` | The application hostname (default: `console.<HostName>`). |
| `-ApiHostName` | The agent and machine API hostname (default: `api.<HostName>`). Its `/` redirects to the console. |
| `-AdditionalHostNames` | Comma-separated DNS aliases for the API, included in the certificate (default: `aiproxy.hopto.me`). Their `/` redirects to the console. HTTP requests keep their hostname when redirected to HTTPS. |
| `-Email` | Your address, for Let's Encrypt. |
| `-Root` | Where nginx, win-acme and the certificates go. |
| `-Backend` | The server nginx forwards to. |
| `-ConsoleBackend` | The static console nginx forwards to (default: `http://127.0.0.1:8081`). |
| `-SiteBackend` | The static brochure site nginx forwards to. |
| `-HttpPort`, `-HttpsPorts` | The ports nginx listens on. `-HttpsPorts` is comma-separated. |
| `-Staging` | Let's Encrypt's staging service: a certificate browsers don't trust, but no rate limits. For testing. |
| `-CertFile`, `-KeyFile` | Use your own certificate instead of Let's Encrypt. |
| `-NoTasks` | Skip the firewall rules and tasks, and the need to run as administrator. |

nginx forwards the `Host` header to each service. The API routes upgrade WebSockets, turn buffering off and allow hour-long reads, so live views, pulls and terminals stream. Requests for any other name get no TLS at all. The templates are `scripts/https/nginx.conf.template` and `nginx-https.conf.template`; on another system, use them as a starting point for your own nginx.

Set `proxy.public_url` to `https://console.<domain>`. The HTTPS script also configures `auth.<domain>` (`-AuthHostName`, `-AuthBackend`) for the separate issuer; supply its registered URLs and shared secret as described in [OIDC](oidc.md).

### HTTPS from the server itself

The server can also serve HTTPS directly: set `proxy.tls.cert` and `proxy.tls.key` in `backends.yaml` to a certificate and key (paths relative to that file's folder). See [Configuration](configuration.md).

## What the server hands out

Each server offers the builds of its own version, so the programs a machine installs from it always speak its protocol. They're public, like `/health`, because a machine needs them before it can sign in.

- `scripts/build-dist.sh` (`.\scripts\build-dist.ps1`) puts a version's builds in `dist/<version>/`, with a `SHA256SUMS` over every file ([Development](development.md#distributions)).
- The server serves `dist/<its version>/` (`AI_PROXY_DIST_DIR` moves `dist/`), or when that wasn't built, the newest folder with the same `X.Y.Z`. Only files `SHA256SUMS` lists are reachable.
- `/downloads/index.json` lists them with their platform, component, kind, size and SHA-256; `/downloads/<version>/<path>` serves each one.
- The console's **Downloads** page (`/get`) offers them per platform, your own computer's and the Android app first: the `agent` and `agent-machine` archives, native packages, the bare binaries, the Python wheel and the Android app, each with its size, SHA-256 and install commands. It's public: the sign-in page and the rail link to it.
- `/android` (and `/downloads/android.apk`) is always the server's Android app, a short address to type on a phone.

## Installing on a machine

### The one-line installers

The quickest way, from the server itself:

```bash
curl -fsSL http://server:9999/install.sh | sh                                      # Linux, macOS, FreeBSD
curl -fsSL http://server:9999/install.sh | sh -s -- --only agent-machine --service # just agent-machine, started at boot
```

```powershell
irm http://server:9999/install.ps1 | iex                                                         # Windows
& ([scriptblock]::Create((irm http://server:9999/install.ps1))) -Only agent-machine -Service
```

The server writes each script for the request: its address as you reached it (checked to be plain URL characters, so a crafted `Host` can't put commands into it), its build's version, and a table of its build's archives. The script works out the computer's system and processor, downloads the archives for it, checks their SHA-256, runs the installer inside them, and starts OIDC device authorization. Approve the printed codes in your browser; the installer never asks for your password.

| `install.sh` | `install.ps1` | What it does |
|---|---|---|
| `--only agent` or `--only agent-machine` | `-Only` | Just one of them (default: both). |
| `--service` | `-Service` | Start `agent-machine` at boot or sign-in (below). |
| `--prefix DIR` | `-Prefix DIR` | Install into `DIR`. |
| `--no-login` | `-NoLogin` | Don't sign in; print the commands instead. |
| `--no-start` | `-NoStart` | Don't start `agent-machine` after signing it in. |

`AGENT_PLATFORM` overrides the detected platform (such as `linux-arm64`). The console's **Downloads** page and **Add a machine** show the commands for your server, ready to copy.

### By hand from an archive

Every archive has an installer for its system. Unpack it, then:

```bash
./install.sh                    # into /usr/local/bin as root, else ~/.local/bin
./install.sh --prefix DIR       # into DIR
sudo ./install.sh --service     # also start agent-machine at boot
./install.sh --uninstall        # remove them again, and the service (sign-ins and settings stay)
```

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1            # into %LOCALAPPDATA%\Programs\agent-proxy, on your PATH
powershell -ExecutionPolicy Bypass -File install.ps1 -Service   # also start agent-machine when you sign in
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

### As a service

`--service` (`-Service`) makes `agent-machine` start by itself. It runs as the user who ran the installer (the `sudo` user under `sudo`), because that user's `agent-machine login` and settings are the ones it uses. If an `agent-machine` is already running, the installer stops it and says so, so the service's own can join the fleet. Sign in with `agent-machine login` before or after; the service keeps trying until then.

| System | What `--service` sets up |
|---|---|
| Linux, as root | A systemd service, `/etc/systemd/system/agent-machine.service`, run as that user from boot |
| Linux, as a user | A systemd user unit, `~/.config/systemd/user/agent-machine.service`, run while you're signed in (`sudo loginctl enable-linger <you>` runs it from boot) |
| macOS | A launchd agent, `~/Library/LaunchAgents/com.ai-coding-agent-proxy.agent-machine.plist`, run when you log in |
| FreeBSD, as root | An rc.d service, `/usr/local/etc/rc.d/agent_machine`, enabled with `sysrc` |
| Windows | A scheduled task, `agent-machine`, that runs `agent-machine start` when you sign in |

### Native packages

Built with `PACKAGES=1` (`-Packages`), they install the same programs:

```bash
sudo apt install ./agent-<version>-linux-amd64.deb               # Debian, Ubuntu
sudo dnf install ./agent-<version>-linux-amd64.rpm               # Fedora, RHEL (warns that it isn't signed)
sudo apk add --allow-untrusted ./agent-<version>-linux-amd64.apk # Alpine
sudo pacman -U ./agent-<version>-linux-amd64.pkg.tar.gz          # Arch
msiexec /i agent-<version>-windows-amd64.msi                     # Windows
sudo installer -pkg agent-<version>-darwin-arm64.pkg -target /   # macOS
```

| Format | Installs to |
|---|---|
| `.deb`, `.rpm`, `.apk`, Arch `.pkg.tar.gz` | `/usr/bin` |
| `.msi` | `Program Files\agent` or `Program Files\agent-machine`, on the system `PATH` |
| macOS `.pkg` | `/usr/local/bin` |

`agent-<version>-...` holds the package named `agent-proxy` (with `agent` and `agent-proxy`), and `agent-machine-<version>-...` the `agent-machine` one. None are signed. The `agent-machine` Linux packages include a systemd user unit, not enabled: after `agent-machine login`, turn it on with `systemctl --user enable --now agent-machine`. The MSI and macOS packages set up no service: run `agent-machine start`, or use the archive's installer with `--service`.

### The Python wheel

For computers without a Go build, `python/` is a drop-in `agent`, `agent-proxy` and `agent-machine` in Python, with the same protocol, files and ports, and everything the Go programs do. The server hands it out as one wheel:

```bash
pip install http://server:9999/downloads/<version>/python/ai_proxy_agent_py-<version>-py3-none-any.whl
```

The **Downloads** page has its exact address. Its `update` pip-installs the server's wheel. See `python/README.md`.

## Versions and updates

The whole project has one version, in `VERSION` at the root. The server, `agent`, `agent-machine`, the Android app and the Python wheel are all stamped with it: `X.Y.Z` for a build of its `vX.Y.Z` tag, otherwise `X.Y.Z+<commit>`, with `.dirty` for uncommitted changes outside `config/`. `AI_PROXY_VERSION` names another. `/health` shows the server's; `agent version` and `agent-machine version` print theirs.

**Compatibility is a protocol level, not the version.** `agent` and `agent-machine` send their protocol with every request. The server accepts a range (`MIN_PROTOCOL` to `PROTOCOL`) and answers anything outside it with `426 Upgrade Required`, saying how to update. Inside the range, a different version just works. Machines on another build show **Update available** on **Machines**.

**The programs update themselves from the server they're signed in to:**

```bash
agent update            # agent and agent-proxy; stops the running agent-proxy so the next agent starts the new one
agent-machine update    # agent-machine; restarts it if it runs in the background
```

Each takes the server's build for its platform from `artifact/`, checks its SHA-256, and replaces itself. A copy in a folder it can't write to, as a package installs, is left to the package manager: update it from the **Downloads** page. With `agent-machine updates on`, the console's **Update now** on a machine's page does the same from there (`POST /api/machines/{id}/update`).

So to roll out a new version: build it on the server's computer (`scripts/build-dist.sh`), restart `agent-server` on the new code, then update each machine.

## Ports

| Port | What listens | Where |
|---|---|---|
| 9999 | `agent-server`: the API, the console, and everything machines and agents send | All interfaces, from `config/backends.yaml` (`8181` without a config) |
| 8184 | Each agent machine's `agent-proxy`: its launcher API and agents' model endpoints | Loopback only (`AI_PROXY_LOCAL_PORT`) |
| 8183 | Each agent machine's `agent-proxy`: the HTTPS proxy for account agents | Loopback only (`AI_PROXY_LOCAL_INTERCEPT_PORT`) |
| 9980, 9443, 9998 | nginx from `setup-https.ps1`: HTTP for Let's Encrypt, HTTPS | All interfaces |
| 11434 | Ollama | On its machine |
| 18500 | Strata | On its machine |
| 12434 | Docker Model Runner | On its machine |
| 5173 | Vite, in development | This computer |

Only the server's port has to be reachable from other machines. Account agents' HTTPS travels over it, tunnelled by each `agent-proxy`, and so does agents' traffic to a machine's model servers when that machine's `agent-machine` can tunnel, so model servers can stay on loopback. The server needs no other open port. The server listens on 8183 itself only when `intercept.listen_port` is set.
