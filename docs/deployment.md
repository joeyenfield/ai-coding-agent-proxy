# Deployment

How to run the server for other machines and people, put HTTPS in front of it, and get `agent` and `agent-machine` onto the computers that use it.

**Authentication is mandatory.** For public deployments, run `agent-auth` separately at your own issuer hostname (`auth.example.com` below), register API callbacks, configure the shared introspection secret and provide HTTPS. `deploy/compose/server/compose.yaml` includes the issuer on host loopback :10000, with its state in its own PostgreSQL schema. See [OIDC authentication](oidc.md) before starting or upgrading public services.

**To try it first**, run the self-contained demo stack on your own computer: [Quickstart with Docker or Podman](quickstart.md), or [Quickstart on Kubernetes](quickstart-kubernetes.md). The demo has its own loopback issuer, signs its machines in through normal device authorization, and mounts no host project folders or Docker socket; `deploy/compose/quickstart/README.md` explains how its local sign-in and Podman setup work. For a public install, follow this page and the [OIDC deployment guide](oidc.md).

**For Kubernetes in production**, and for running several server processes behind a load balancer (high availability, or hundreds of engineers), see [Running several server processes](scaling.md) and the Helm chart in `deploy/helm/agent-mesh/`.

## The server in Docker

The server and its console are one image, `ghcr.io/joeyenfield/agent-server`; agent-auth runs from the same image. `deploy/compose/server/compose.yaml` runs them with PostgreSQL and the brochure site, pulling the published images ([Images on ghcr.io](#images-on-ghcrio)). Run Compose from that folder, with its `.env` beside it:

```bash
cd deploy/compose/server
docker compose up -d                                    # the last promoted stable images (latest)
AI_PROXY_IMAGE_TAG=2.0.1 docker compose up -d           # or a release of your choice
docker compose pull && docker compose up -d             # move to the newest build
```

**Moving from the old root `compose.yaml`.** It moved to `deploy/compose/server/`. Compose names a project, and so its volumes, after the folder it runs from, so set `COMPOSE_PROJECT_NAME` to your checkout folder's name (the name the volumes have, such as `ai-proxy` for `ai-proxy_postgres-data`; `docker volume ls` shows them) in the `.env`, and move that `.env` beside the new file. `devhelpers/release_scripts/deploy.sh` does this for you with `-p`.

Pick the tag to match the `dist/` build you hand out (below): the server offers the build of its own version. To build the images from your checkout instead, add `devhelpers/server/compose.build.yaml`, from the repository root:

```bash
AI_PROXY_VERSION=2.0.1 docker compose -f deploy/compose/server/compose.yaml -f devhelpers/server/compose.build.yaml up -d --build
docker build -f server/Dockerfile -t agent-server .     # or one image by hand
```

The image builds the console with Node, then installs the server on `python:3.13-slim`. It runs as a non-root user (`agent`, uid 10001), listens on `0.0.0.0:9999`, and checks `/health` every 30 seconds. It sets `AI_PROXY_HOME=/app`, `AI_PROXY_HOST=0.0.0.0` and `AI_PROXY_UI_BUILD=0`.

The `marketing-site` service serves `site/` on `127.0.0.1:8080` for the reverse proxy. The public website uses the apex and `www` hostnames, the application console uses `console.<domain>`, and agent and machine clients can use `api.<domain>`.

Everything the server keeps is in PostgreSQL ([below](#postgresql)), so its own volumes hold little:

| Volume | What it holds |
|---|---|
| `postgres-data` | The `postgres` service's database: settings, agents, accounts, the fleet, every session's records, payloads and terminals, and agent-auth's state. |
| `/app/logs` | The server's own log (`proxy/proxy.log`) and the capture CA's certificate files for agents. |
| `/app/certs` | `first-user-password`, once. |
| `/app/config`, `/app/auth-state` | Only for an install from before the database: read once, on the first start, to import its files. A new install doesn't need them. |
| `/app/dist` | The builds to hand out (`deploy/compose/server/compose.yaml` mounts the checkout's `dist/`). Fill it with `docker compose exec --user 0 agent-server agent-server downloads fetch`, or with your own build. |

A new server starts with the hosted Anthropic API as its one provider. Local providers come from machines in the fleet: run `agent-machine` where the models are and add them from the console ([Machines](machines.md)).

Authentication is always required. The first start on a new database creates an `admin` user with a random password. `docker compose logs agent-server` shows it, and it's saved as `first-user-password` in the certs volume. See [Accounts](accounts.md).

**Versions in Docker.** The published images carry their version (`2.1.61`, or `2.1.61+snapshot.<timestamp>.g<commit>` for a snapshot). An image you build has no git inside, so pass the version as a build argument: `AI_PROXY_VERSION=1.2.0+abc1234 docker compose -f deploy/compose/server/compose.yaml -f devhelpers/server/compose.build.yaml build`. The server hands out the `dist/<version>/` of its own version, or the newest of the same `X.Y.Z` when that wasn't built, so match it to the builds you mount.

**A sandbox machine.** `docker compose --profile sandbox up -d` also starts `agent-sandbox` (`ghcr.io/joeyenfield/agent-sandbox`, from `sandbox/Dockerfile`): `agent`, `agent-machine` and npm coding agents in a container that joins the fleet and takes launches in `/workspace` only. Set `AI_PROXY_SERVER` to your HTTPS API and optionally `AI_PROXY_MACHINE_NAME`. Approve both device codes from its logs on first start, or set `AI_PROXY_MACHINE_SETUP_CODE` to a setup code from the console's **Machines** page (**Add a machine** › **Create a setup code**) so agent-machine signs in without one; persistent credentials refresh on later starts. Password/key bootstrap is removed. See [Machines](machines.md).

**Podman** runs the same images; when building them yourself, pass `--http-proxy=false` to `podman build`. `podman compose` works with any Compose provider, a Docker Compose one included: it connects it to the Podman engine. Keep the root `.dockerignore`'s exclusions (generated screenshot and test folders, virtual environments, agent history) when you customize the build context: private Windows ACLs on generated folders otherwise stop a build with `checking context: can't stat`. On Windows, a `netavark`/`nftables` failure before containers start can be a WSL kernel problem ([reported upstream](https://github.com/containers/netavark/issues/1495)): run `wsl --update`, then restart WSL and Podman (`wsl --shutdown` stops every distribution, so save other WSL work first). Keep bridge networking and the VM firewall as they are.

### Images on ghcr.io

The [local release command](releases.md) builds the selected images on your computer and optionally pushes them directly to a registry. No GitHub Actions runner is used. The default platform is `linux/amd64`; add `--platform linux/arm64` for ARM64 (a Raspberry Pi). The available images are:

| Image | Built from | What it is |
|---|---|---|
| `ghcr.io/joeyenfield/agent-server` | `server/Dockerfile` | agent-server and its console; agent-auth is the same image with `entrypoint: [agent-auth]` |
| `ghcr.io/joeyenfield/agent-sandbox` | `sandbox/Dockerfile` | A fleet machine in a container, with the coding agents ([Machines](machines.md)) |
| `ghcr.io/joeyenfield/ai-control-mesh-site` | `site/Dockerfile` | The brochure website |
| `ghcr.io/joeyenfield/ai-control-mesh` | `devhelpers/allinone/Dockerfile` | The whole system in one container, to try it: the server, sign-in and console, and a machine with the coding agents ([Run it on your own computer](local.md#try-it-in-one-container)) |
| `ghcr.io/joeyenfield/ai-control-mesh-gpu` | `devhelpers/allinone/Dockerfile` with `OLLAMA_GPU=cuda_v13` | The same, with the CUDA libraries for its model server, for a container that is given an NVIDIA GPU ([GPUs in containers](gpu.md)) |

Snapshot builds get unique `snapshot-<timestamp>-g<commit>` tags and versions `X.Y.Z+snapshot.<timestamp>.g<commit>`. Stable builds get `X.Y.Z`. Only explicit stable promotion updates `latest`; pushes to `master` and Git tags do not publish anything. Existing older tags remain usable. The standalone console (`ui/Dockerfile`) is built with its deployment's API address (`VITE_API_URL`) on the deployment host.

```bash
docker pull ghcr.io/joeyenfield/agent-server:2.0.1
```

Everything under `deploy/` uses these. These variables choose them:

| Variable | What it does |
|---|---|
| `AI_PROXY_IMAGE_TAG` | The services tag to run (default `latest`, the last promoted build): `2.0.1` or a unique snapshot tag. |
| `AI_PROXY_SITE_IMAGE_TAG` | The website tag in the server Compose stack, independent of services (default `latest`). |
| `AI_PROXY_IMAGES` | Where the images are (default `ghcr.io/joeyenfield`): set it to your own registry or fork's owner, such as `ghcr.io/<you>`. |

The server still hands out the agent and agent-machine builds from its `dist/` folder, which the image doesn't contain: mount it as before (`/app/dist`), built from the same commit as the image or fetched from its release with `agent-server downloads fetch`.

## PostgreSQL

`deploy/compose/server/compose.yaml` runs PostgreSQL 17 as the `postgres` service, with no published port: only `agent-server` and `agent-auth` reach it. Set three passwords in `.env` beside it before the first start, using letters and digits:

```bash
AI_PROXY_POSTGRES_PASSWORD=...   # PostgreSQL's administrator, postgres
AI_PROXY_DB_API_PASSWORD=...     # agent-server's role, mesh_api
AI_PROXY_DB_AUTH_PASSWORD=...    # agent-auth's role, mesh_auth
```

On its first start the service runs `server/docker/postgres-init.sh`, which makes a role for each program and a schema each owns:

| Role | Schema | What it can do |
|---|---|---|
| `mesh_api` | `mesh` | Everything the server keeps. It can't read agent-auth's schema. |
| `mesh_auth` | `mesh_auth` | agent-auth's OAuth state and signing key. In `mesh` it may only read the accounts (`users`, `orgs`, `meta`); the server grants that itself each time it migrates. |

`agent-server` gets `AI_PROXY_DATABASE_URL=postgresql+psycopg://mesh_api:...@postgres:5432/mesh` and `agent-auth` gets `AI_PROXY_AUTH_DATABASE_URL=postgresql+psycopg://mesh_auth:...@postgres:5432/mesh`. Both wait for the database to be healthy, and the server brings its tables up to date on every start.

**Upgrading from files.** An install from before the database keeps its old volumes (`agent-server-config`, `agent-server-logs`, `agent-server-certs`, `agent-auth-state`), and `deploy/compose/server/compose.yaml` still mounts them. On the first start with PostgreSQL, the server imports `backends.yaml`, `models.yaml`, `agents.yaml`, `users.json`, the keys, the capture CA, `machines.json` and every recorded session, and agent-auth imports its `issuer.sqlite3` and signing key, so everyone stays signed in. A large `logs/` takes a while; the server logs its progress and carries on after an interruption. Nothing is deleted. Once it runs from the database you can drop the old files, and the `/app/config` and `/app/auth-state` mounts. See [Upgrading from files](configuration.md#upgrading-from-files).

### A PostgreSQL of your own

Make a database named `mesh` (`CREATE DATABASE mesh;`), then run the same statements in it as its administrator, with your own passwords, and give the two programs their URLs:

```sql
CREATE ROLE mesh_api LOGIN PASSWORD '...';
CREATE ROLE mesh_auth LOGIN PASSWORD '...';
CREATE SCHEMA mesh AUTHORIZATION mesh_api;
CREATE SCHEMA mesh_auth AUTHORIZATION mesh_auth;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE mesh FROM PUBLIC;
GRANT CONNECT ON DATABASE mesh TO mesh_api, mesh_auth;
```

Keep these role and schema names: the server grants `mesh_auth` its read access by name. Start `agent-server` once before `agent-auth`'s first sign-in: until the server has made its tables and granted `mesh_auth` read access, agent-auth finds no accounts.

### Backups

One dump holds everything, including the secrets (the capture CA's key, agent-auth's signing key and hashed tokens). Keep it as private as the server:

```bash
docker compose exec -T postgres pg_dump -U postgres -Fc mesh > mesh.dump                          # back up
docker compose exec -T postgres pg_restore -U postgres -d mesh --clean --if-exists < mesh.dump    # restore
```

To copy only the settings (no accounts or recordings), use `agent-server config export` and `import` ([Configuration](configuration.md#all-settings-as-one-document)).

## HTTPS

Put HTTPS in front of the server whenever it's reachable from outside your own network. Without it, sign-in cookies, tokens and recorded traffic cross the network in the clear. What any reverse proxy in front needs to do (WebSockets, no buffering, long timeouts, `Host` and `X-Forwarded-For`) is in [Networking](networking.md#behind-a-reverse-proxy). [Security](security.md#a-checklist-for-a-public-server) has a checklist for a public server.

**HTTPS from the server itself.** The server can also serve HTTPS directly: set `AI_PROXY_TLS_CERT` and `AI_PROXY_TLS_KEY` (or `proxy.tls` in its settings) to a certificate and key file. See [Configuration](configuration.md).

### Separate console and API domains

The console can run as static files, independently of `agent-server`:

| Hostname | Service |
|---|---|
| `example.com`, `www.example.com` | Brochure (`marketing-site`, loopback port 8080). |
| `console.example.com` | Static console (`console`, loopback port 8081). No API forwarding. |
| `api.example.com` | `agent-server` (port 9999): API, WebSockets, live streams, documentation files, downloads and installers. |
| `auth.example.com` | `agent-auth` (loopback port 10000): OIDC sign-in, device approval and OAuth endpoints. |

Set these variables for Compose (or put them in the root `.env`, which is ignored by git), then run `docker compose --profile console up -d --build` (`--build` builds only the console, with its API's address; the rest are pulled):

```dotenv
VITE_API_URL=https://api.example.com
AI_PROXY_SERVE_UI=0
AI_PROXY_CONSOLE_ORIGINS=https://console.example.com
AI_PROXY_OIDC_INTROSPECTION_SECRET=<a long random secret>
# Optional: lets people store Copilot tokens for their subscriptions (agent-server vault key makes one).
# Keep it out of database backups; losing it makes stored tokens unreadable.
AI_PROXY_VAULT_KEYS=
# nginx on the same computer terminates HTTPS:
AI_PROXY_PUBLISH_HOST=127.0.0.1
AI_PROXY_FORWARDED_ALLOW_IPS=*
```

**Behind a reverse proxy on the same computer**, set the last two. Inside a container, requests come from Docker's bridge address, not 127.0.0.1, so by default the server ignores the proxy's `X-Forwarded-For` and `X-Forwarded-Proto`. Every visitor then shows the bridge's address (machines included), and the API builds `http://` sign-in callbacks. `AI_PROXY_FORWARDED_ALLOW_IPS=*` makes those headers count, and `AI_PROXY_PUBLISH_HOST=127.0.0.1` publishes port 9999 on loopback only, so nothing but the proxy can send them. `agent-auth` does this already: its port is loopback-only. Behind Cloudflare, also have nginx take the visitor's address from `CF-Connecting-IP` for Cloudflare's ranges only (`set_real_ip_from`, `real_ip_header`), or every address is Cloudflare's.

`VITE_API_URL` is a **build-time** setting. Rebuild the console when its API address changes. `ui/Dockerfile` builds it; `ui/nginx.conf` serves its assets and SPA routes, returns 404 for API paths and never forwards requests to the server. With no `VITE_API_URL`, a normal `npm run build` still uses the page's origin for bundled/local installs.

On the API server, `AI_PROXY_SERVE_UI=0` disables console routes, static assets and automatic UI rebuilding. `AI_PROXY_CONSOLE_ORIGINS` is a comma-separated list of exact trusted origins, including the scheme and any port, without paths or wildcards. The same list controls credentialed CORS, mutation origin checks and WebSocket origin checks. Leave it empty for same-origin-only installs.

Keep the console and API on HTTPS subdomains of the **same site**. Sign-in sets an HttpOnly, host-only cookie on the API domain; the console includes credentials on requests and live streams, and browsers send that cookie on API WebSockets. The cookie is not shared with the brochure or console hosts. Unrelated registrable domains are not supported by this SameSite=Lax cookie setup. Moving an existing console to the API host's cookie requires signing in again.

Set `proxy.public_url` to the console's HTTPS address. CLI installation and login use the API hostname (`https://api.example.com`); OIDC uses the issuer hostname (`https://auth.example.com`). Include all hostnames in DNS and TLS. With Cloudflare, use Full (strict), and never cache API or auth responses.

### nginx and Let's Encrypt on Windows

`devhelpers/scripts/https/setup-https.ps1` sets up nginx with a Let's Encrypt certificate that renews itself. It serves the brochure site on the apex and `www` hostnames, the application on `console.<domain>`, agent/machine API requests on `api.<domain>`, and the OIDC issuer on `auth.<domain>`. Point all five DNS names to this host, start the Compose services, then run it once from an elevated PowerShell; rerun it any time, and it picks up where things are:

```powershell
powershell -ExecutionPolicy Bypass -File devhelpers\scripts\https\setup-https.ps1 -HostName aicontrolmesh.com -AdditionalHostNames aiproxy.hopto.me -Email you@example.net
```

It installs, under `-Root` (default `C:\ProgramData\agent-proxy-https`):

- **nginx** for Windows, serving plain HTTP on `-HttpPort` (9980) for Let's Encrypt's check, and HTTPS on `-HttpsPorts` (9443 and 9998). The apex and `www` proxy to `-SiteBackend` (`http://127.0.0.1:8080`), the console to `-ConsoleBackend` (`http://127.0.0.1:8081`), the API to `-Backend` (`http://127.0.0.1:9999`), and the issuer to `-AuthBackend` (`http://127.0.0.1:10000`);
- **win-acme**, the Let's Encrypt client, which proves the name with a file nginx serves under `/.well-known/acme-challenge/`, writes the certificate, and renews it from its own scheduled task, reloading nginx;
- a scheduled task, `agent-proxy-https nginx`, that starts nginx at boot, and firewall rules for the ports.

The name has to reach this computer from the internet on port 80 for Let's Encrypt (forward the router's 80 to `-HttpPort`), and on 443 for people (to the first of `-HttpsPorts`).

| Option | What it does |
|---|---|
| `-HostName` | The primary DNS name to get a certificate for (default: `aicontrolmesh.com`). |
| `-ConsoleHostName` | The application hostname (default: `console.<HostName>`). |
| `-ApiHostName` | The agent and machine API hostname (default: `api.<HostName>`). Its `/` redirects to the console. |
| `-AuthHostName` | The OIDC issuer's hostname (default: `auth.<HostName>`). |
| `-AdditionalHostNames` | Comma-separated DNS aliases for the API, included in the certificate (default: `aiproxy.hopto.me`). Their `/` redirects to the console. HTTP requests keep their hostname when redirected to HTTPS. |
| `-Email` | Your address, for Let's Encrypt. |
| `-Root` | Where nginx, win-acme and the certificates go. |
| `-Backend` | The server nginx forwards to. |
| `-AuthBackend` | The issuer nginx forwards to (default: `http://127.0.0.1:10000`). |
| `-ConsoleBackend` | The static console nginx forwards to (default: `http://127.0.0.1:8081`). |
| `-SiteBackend` | The static brochure site nginx forwards to. |
| `-HttpPort`, `-HttpsPorts` | The ports nginx listens on. `-HttpsPorts` is comma-separated. |
| `-Staging` | Let's Encrypt's staging service: a certificate browsers don't trust, but no rate limits. For testing. |
| `-CertFile`, `-KeyFile` | Use your own certificate instead of Let's Encrypt. |
| `-NoTasks` | Skip the firewall rules and tasks, and the need to run as administrator. |

nginx forwards the `Host` header to each service. The API routes upgrade WebSockets, turn buffering off and allow hour-long reads, so live views, pulls and terminals stream. Requests for any other name get no TLS at all. The templates are `devhelpers/scripts/https/nginx.conf.template` and `nginx-https.conf.template`; on another system, use them as a starting point for your own nginx.

Set `proxy.public_url` to `https://console.<domain>`. The HTTPS script also configures `auth.<domain>` (`-AuthHostName`, `-AuthBackend`) for the separate issuer; supply its registered URLs and shared secret as described in [OIDC](oidc.md).

## What the server hands out

Each server offers the builds of its own version, so the programs a machine installs from it always speak its protocol. They're public, like `/health`, because a machine needs them before it can sign in.

- `devhelpers/scripts/build-dist.sh` (`.\devhelpers\scripts\build-dist.ps1`) puts a version's builds in `dist/<version>/`, with a `SHA256SUMS` over every file ([Development](development.md#distributions)).
- `agent-server downloads fetch` fills `dist/<X.Y.Z>/` from a GitHub Release instead, with no Go and no checkout: every archive and the wheel, each checked against the release's `SHA256SUMS`, and the programs unpacked for `agent update`. `--version` names another release than the server's own, `--from <url>` another place that holds a release's files. In Compose: `docker compose exec --user 0 agent-server agent-server downloads fetch`.
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
| `--service` | `-Service` | Start `agent-machine` at boot or sign-in ([below](#from-an-archive-and-as-a-service)). |
| `--prefix DIR` | `-Prefix DIR` | Install into `DIR`. |
| `--no-login` | `-NoLogin` | Don't sign in; print the commands instead. |
| `--no-start` | `-NoStart` | Don't start `agent-machine` after signing it in. |
| `--python` | `-Python` | Install the Python versions instead ([below](#native-packages-and-the-python-wheel)). |
| `--no-tray` | `-NoTray` | Leave out the tray icon (below). |

**On any system, with Python.** With `--python`, or when the server has no Go build for the computer's system or processor, the installer installs the Python wheel instead. That works anywhere with Python 3.9 or later. It checks the wheel's SHA-256 and installs it into an environment of its own: `~/.local/share/agent-py` (`/usr/local/lib/agent-py` as root, `%LOCALAPPDATA%\agent-py` on Windows). Then it puts `agent`, `agent-proxy` and `agent-machine` in the prefix, as links on Linux, macOS and FreeBSD and as copies of pip's launchers on Windows. Signing in is the same. `--service` sets up the Go `agent-machine` only, so start the Python one with `agent-machine start`.

**The tray icon.** On a desktop (Windows, macOS, or Linux and BSD with a display), not as root, and with Python 3.9 or later, the installer also puts in `agent-tray`. It comes from the same wheel and environment, plus `pystray` and `Pillow` from PyPI. Only `agent-tray` is linked into the prefix, so the Go programs stay the ones you run. It starts when you sign in to the computer, and the installer starts it once. The icon shows whether this machine is in the fleet and whether agent is signed in, and starts and stops them ([The tray icon](local.md#the-tray-icon)). If it can't be installed, the installer says so and everything else works the same.

`AGENT_PLATFORM` overrides the detected platform (such as `linux-arm64`). The console's **Downloads** page and **Add a machine** show the commands for your server, ready to copy.

### From an archive, and as a service

Every archive has an installer for its system. Unpack it, then:

```bash
./install.sh                    # into /usr/local/bin as root, else ~/.local/bin
./install.sh --prefix DIR       # into DIR
sudo ./install.sh --service     # also start agent-machine at boot
./install.sh --uninstall        # remove them again, and the service (runs uninstall.sh; see Uninstalling)
```

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1            # into %LOCALAPPDATA%\Programs\agent-proxy, on your PATH
powershell -ExecutionPolicy Bypass -File install.ps1 -Service   # also start agent-machine when you sign in
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall # runs uninstall.ps1
```

`--service` (`-Service`) makes `agent-machine` start by itself. It runs as the user who ran the installer (the `sudo` user under `sudo`), because that user's `agent-machine login` and settings are the ones it uses. If an `agent-machine` is already running, the installer stops it and says so, so the service's own can join the fleet. Sign in with `agent-machine login` before or after; the service keeps trying until then.

| System | What `--service` sets up |
|---|---|
| Linux, as root | A systemd service, `/etc/systemd/system/agent-machine.service`, run as that user from boot |
| Linux, as a user | A systemd user unit, `~/.config/systemd/user/agent-machine.service`, run while you're signed in (`sudo loginctl enable-linger <you>` runs it from boot) |
| macOS | A launchd agent, `~/Library/LaunchAgents/com.ai-control-mesh.agent-machine.plist`, run when you log in |
| FreeBSD, as root | An rc.d service, `/usr/local/etc/rc.d/agent_machine`, enabled with `sysrc` |
| Windows | A scheduled task, `agent-machine`, that runs `agent-machine start` when you sign in |

### Native packages and the Python wheel

Built with `PACKAGES=1` (`-Packages`), native packages install the same programs:

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

**The Python wheel.** For computers without a Go build, `python/` is a drop-in `agent`, `agent-proxy` and `agent-machine` in Python, with the same protocol, files and ports, and everything the Go programs do. The server hands it out as one wheel:

```bash
pip install http://server:9999/downloads/<version>/python/ai_proxy_agent_py-<version>-py3-none-any.whl
```

The **Downloads** page has its exact address. Its `update` pip-installs the server's wheel. See `python/README.md`.

### Uninstalling

The server serves an uninstaller beside each installer. It needs no build, and it removes what the installers put on the computer whichever server they came from:

```bash
curl -fsSL http://server:9999/uninstall.sh | sh                  # Linux, macOS, FreeBSD
curl -fsSL http://server:9999/uninstall.sh | sh -s -- --purge    # also sign out and delete settings
sudo sh uninstall.sh                                             # what was installed as root (/usr/local/bin, the system service)
```

```powershell
irm http://server:9999/uninstall.ps1 | iex                                       # Windows
& ([scriptblock]::Create((irm http://server:9999/uninstall.ps1))) -Purge
```

Every archive has the same script (`uninstall.sh` or `uninstall.ps1`), which `install.sh --uninstall` and `install.ps1 -Uninstall` run. In order, it:

1. Removes agent-machine's service (the systemd unit, launchd agent, rc.d script or scheduled task from `--service`) and stops an `agent-machine` you started yourself.
2. Stops this install's `agent-proxy` (`agent proxy stop`, else by its path). Agents running through it lose their connection to the server.
3. With `--purge`, signs `agent` and `agent-machine` out of every server they're signed in to, which revokes their tokens at the issuer.
4. As root on Linux, removes what `sudo agent-machine servers service` set up: the sudoers rule, the `ollama.service` drop-in and `/etc/agent-machine/ollama.env`. Ollama's service stays.
5. Deletes the programs, and on Windows the PATH entry and the empty folder.
6. With `--purge`, deletes their settings, sign-ins, logs and caches: `agent-proxy` and `agent-machine` in your config folder (`~/.config`, or `%APPDATA%`) and `~/.cache/agent-proxy`. A folder named by `AI_PROXY_MACHINE_HOME` is left for you to delete. First it stops the Ollama agent-machine started itself (the one in its `servers.json`, which on Windows keeps `ollama.log` there open), with its model runners; an Ollama you started yourself, or `ollama.service`, keeps running.
7. Says how to remove copies installed some other way: a package (`apt remove agent-proxy agent-machine` and the like), an `.msi` (Settings › Apps), the Python version (`pip uninstall ai-proxy-agent-py`), or another folder (`--prefix`).

| `uninstall.sh` | `uninstall.ps1` | What it does |
|---|---|---|
| `--only agent` or `--only agent-machine` | `-Only` | Remove just one of them (default: both). |
| `--prefix DIR` | `-Prefix DIR` | The folder they're in (default: `/usr/local/bin` as root, else `~/.local/bin`; `%LOCALAPPDATA%\Programs\agent-proxy` on Windows). |
| `--purge` | `-Purge` | Also sign out and delete settings, sign-ins, logs and caches. |

Without `--purge`, sign-ins and settings stay, so reinstalling picks up where you left off. Containers agent-machine ran, and the models it pulled, stay too. The machine stays in the server's fleet list until someone removes it there (the machine's page, **Remove from this server**; see [Machines](machines.md#removing-and-restoring-a-machine)). On Windows, a program that's still in use (an agent running through `agent.exe`) can't be deleted: the script says which, so close it and run the script again. The one-line installer's Python environment (`agent-py`, above) stays too: delete its folder after the uninstaller removed the links or launchers.

## Versions and updates

The whole project has one version, in `VERSION` at the root. The server, `agent`, `agent-machine`, the Android app and the Python wheel are all stamped with it: `X.Y.Z` for a build of its `vX.Y.Z` tag, otherwise `X.Y.Z+<commit>`, with `.dirty` for uncommitted changes to tracked files. `AI_PROXY_VERSION` names another. `/health` shows the server's; `agent version` and `agent-machine version` print theirs.

**Compatibility is a protocol level, not the version.** `agent` and `agent-machine` send their protocol with every request. The server accepts a range (`MIN_PROTOCOL` to `PROTOCOL`) and answers anything outside it with `426 Upgrade Required`, saying how to update. Inside the range, a different version just works. Machines on another build show **Update available** on **Machines**.

**The programs update themselves from the server they're signed in to:**

```bash
agent update            # agent and agent-proxy; stops the running agent-proxy so the next agent starts the new one
agent-machine update    # agent-machine; restarts it if it runs in the background
```

Each takes the server's build for its platform from `artifact/`, checks its SHA-256, and replaces itself. A copy in a folder it can't write to, as a package installs, is left to the package manager: update it from the **Downloads** page. With `agent-machine updates on`, the console's **Update now** on a machine's page does the same from there (`POST /api/machines/{id}/update`).

So to roll out a new version: build it on the server's computer (`devhelpers/scripts/build-dist.sh`) or fetch its release (`agent-server downloads fetch`), restart `agent-server` on the new code, then update each machine.

## Ports

| Port | What listens | Where |
|---|---|---|
| 9999 | `agent-server`: the API, the console, and everything machines and agents send | All interfaces, from the stored `proxy` setting (`AI_PROXY_HOST`, `AI_PROXY_PORT`) |
| 10000 | `agent-auth`, the OIDC issuer, behind the reverse proxy as `auth.<domain>` | Loopback (`AI_PROXY_AUTH_HOST`, `AI_PROXY_AUTH_PORT`) |
| 8080, 8081 | The brochure site and the standalone console in Compose, behind the reverse proxy | Loopback |
| 8184 | Each agent machine's `agent-proxy`: its launcher API and agents' model endpoints | Loopback only (`AI_PROXY_LOCAL_PORT`) |
| 8183 | Each agent machine's `agent-proxy`: the HTTPS proxy for account agents | Loopback only (`AI_PROXY_LOCAL_INTERCEPT_PORT`) |
| 9980, 9443, 9998 | nginx from `setup-https.ps1`: HTTP for Let's Encrypt, HTTPS | All interfaces |
| 11434 | Ollama | On its machine |
| 18500 | Strata | On its machine |
| 9931 | llama.cpp's `llama-server` | Loopback on its machine |
| 12434 | Docker Model Runner | On its machine |
| 5173 | Vite, in development | This computer |
| 19999, 20000 | The quickstart's API and issuer | Loopback |

Only the server's port, and the issuer's for signing in (through the reverse proxy), have to be reachable from other machines. Account agents' HTTPS travels over it, tunnelled by each `agent-proxy`, and so does agents' traffic to a machine's model servers when that machine's `agent-machine` can tunnel, so model servers can stay on loopback. The server needs no other open port. The server listens on 8183 itself only when `intercept.listen_port` is set.
