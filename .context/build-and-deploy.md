# Build and deploy (`tools/`, `scripts/`, `server/Dockerfile`, `ui/Dockerfile`, `site/`, `compose.yaml`, `sandbox/`, `quickstart/`)

How the components are versioned, built for every platform, handed out by the server, installed and run in containers. The static brochure site in `site/` is separate from the application console in `ui/`; Compose serves it privately on port 8080 for the public reverse proxy.

## Design in one paragraph

There is one hand-written version (`VERSION`) and one compatibility number (`PROTOCOL`). `tools/dist` builds every Go program for every platform, plus packages, the Python wheel and the Android APK, into `dist/<version>/` with a `SHA256SUMS`. The server hands out exactly those files (`/downloads/`), and every installer and self-update checks them against that list. So a computer only ever installs what its server built.

## Versions

- `VERSION` = `X.Y.Z`. The build version is `X.Y.Z` on its tag, else `X.Y.Z+<commit>[.dirty]`. `.dirty` means uncommitted changes to tracked files outside `config/`; there's no `.git` → `+unknown`. `AI_PROXY_VERSION` overrides it.
- It is computed in three places that must agree: `tools/dist/version.go` (`BuildVersion`), `server/agent_server/version.py` (`build_version`) and `python/agent_py/common.py` (`checkout_version`).
- `PROTOCOL`/`MIN_PROTOCOL` live in `version.py`, `settings.Protocol` in client/ and machine/, and `common.PROTOCOL`; `test_client.py` checks they agree. Raise `PROTOCOL` for changes older programs can't follow, and `MIN_PROTOCOL` only when dropping the old one. Outside the range the server answers 426 (WebSocket close 4426).

## `tools/dist` (its own Go module, no dependencies)

```mermaid
flowchart LR
  src["client/ · machine/ · python/ · android/"] --> dist["go run ./dist"]
  dist --> out["dist/&lt;version&gt;/"]
  out --> a["&lt;os&gt;-&lt;arch&gt;/artifact/ binaries + install.sh|ps1"]
  out --> b["&lt;os&gt;-&lt;arch&gt;/*.zip | *.tar.gz"]
  out --> c["packages: .deb .rpm .apk Arch (Go), .msi (wix), .pkg (Mac)"]
  out --> d["python/*.whl · android/*.apk"]
  out --> e["SHA256SUMS"]
```

- 15 targets (Windows, Linux incl. arm/riscv64/s390x/loong64, macOS, FreeBSD), built with `CGO_ENABLED=0 -trimpath` and `-X main.version`. Components: `agent` (agent and agent-proxy) and `agent-machine`.
- Archives and Linux packages are written in Go (execute bits survive NTFS). A missing external tool (wix, pkgbuild, a JDK or Android SDK) skips its format with a note.
- The wheel is written in Go from `python/pyproject.toml`, with `_version.py` stamped in. It is skipped when the version isn't PEP 440.
- Flags: `-version`, `-root`, `-out`, `-only`, `-packages`, `-android`, `-python`, `-print-version` (used by the build scripts).
- The APK is a release build only when `android/keystore.properties` exists, else a debug build (which carries the Android Auto service); see `android.md`.

## Handing out and installing

- `downloads.py` serves `dist/<server version>/` (or the newest with the same `X.Y.Z`), only files in `SHA256SUMS`, publicly, at `/downloads/...`. `/downloads/index.json` is what `agent update` and `agent-machine update` read.
- **One-liners** `/install.sh` and `/install.ps1` (`installer.py`, templates in `server/agent_server/install/`): the server fills in its address (checked against `SAFE_URL`) and the archive table. They download, check hashes, and run the archive's own installer with `AI_PROXY_ONE_LINER=1`, then sign in from `/dev/tty`.
- **Archive installers** (`tools/dist/install/`): `install.sh` (to `/usr/local/bin` or `~/.local/bin`; `--service` sets up systemd, launchd or rc.d; `--uninstall`) and `install.ps1` (to `%LOCALAPPDATA%\Programs\agent-proxy`, user PATH, `-Service` as an at-logon task).
- Test installers and services as a throwaway user in another WSL distro, never as the Ubuntu user (it runs a real agent-machine).

## Containers

- The root `.dockerignore` excludes local virtual environments, agent metadata/history and generated `.screenshot*`, `.test-*` and `.tmp-*` directories. Keep these out of the context: Compose's Windows context validation can fail on their private ACLs before the container engine sees the build.
- `server/Dockerfile` (built from the repo root, because it needs `ui/`): builds the console with Node, then a python:3.13-slim image running as uid 10001, with `docs/` for `/help` and `AI_PROXY_UI_BUILD=0`. Volumes `/app/config`, `/app/logs`, `/app/certs` (and `/app/auth-state`, owned for `agent-auth`); port 9999. The entrypoint (`server/docker/entrypoint.sh`) seeds `config/` from `/app/defaults/config` without overwriting. Pass `--build-arg AI_PROXY_VERSION` to match a `dist/` folder (no git in the image).
- `compose.yaml`: `marketing-site` on host loopback port 8080, the standalone static `console` on loopback 8081 under `--profile console` (built with `VITE_API_URL`, default `http://localhost:9999`), `agent-auth` on loopback 10000, `agent-server` with named volumes and `./dist:/app/dist:ro`, and `agent-sandbox` under `--profile sandbox`. Split deployments build the console with `VITE_API_URL`, run the API with `AI_PROXY_SERVE_UI=0` and trust the console through `AI_PROXY_CONSOLE_ORIGINS`.
- `site/`: static public brochure site (`index.html`, `styles.css`), served by its own small nginx image (`site/Dockerfile`, `site/nginx.conf`, with a `/health`). The apex and `www` hostnames serve this site; `console.<domain>` serves only the static console (`ui/Dockerfile`, `ui/nginx.conf`); `api.<domain>` routes console, agent and machine requests to `agent-server` and redirects its root to the console.
- The brochure explains the component roles, request routes, organization features and deployment steps. Its architecture and request-flow diagrams are semantic HTML/CSS (no JavaScript or third-party renderer). On phones the request path stacks vertically, with separate console access below it. Keep product claims in step with `docs/`; the hero workspace is illustrative, not live fleet data.
- Bump the brochure stylesheet URL's `v` query in `site/index.html` when changing `site/styles.css`: public CDNs may keep the previous CSS after a deployment.
- `sandbox/`: an image with `agent`, `agent-machine` and the npm coding agents. Its entrypoint signs both in through device approval, sets launches on for `/workspace` only (containers and servers off), and runs `agent-machine run`.
- Mandatory OIDC adds Compose `agent-auth` (same Python image, separate entrypoint), host loopback :10000, private `agent-auth-state` volume, and read-only shared accounts/config. Both services require the same `AI_PROXY_OIDC_INTROSPECTION_SECRET`. HTTPS setup provisions `auth.<domain>` separately; auth responses are uncached and auth URLs excluded from access logs.
- Sandbox first start prints two device codes for browser approval; persistent home credentials refresh later. Password/key bootstrap is removed.
- `quickstart/compose.yaml` is a separate local-demo project: server + issuer, two sandbox-based fleet machines, and Ollama in its own container, which machine-1's `start.py` relays to its loopback port 11434 (agent-machine only discovers loopback model servers). Published ports bind host loopback (19999 API, 20000 issuer). Each machine has separate home/workspace volumes; Ollama has its own model volume. The environment-defined admin defaults to admin/secret, optionally overridden by `QUICKSTART_ADMIN_PASSWORD`.
- The quickstart supports `podman compose` and `docker compose`. Built images use `localhost/` names, pulls and the server/sandbox base images use explicit registries, and shared helper bind mounts use `:ro,z` for SELinux. Podman needs a Compose provider (Docker Compose is supported); Windows/macOS also need a running Podman machine. Services must retain separate network namespaces: Podman refuses to recreate a container another shares (`network_mode: service:`) with. The issuer's `/health` is what `auth`'s healthcheck, and so the machines' `depends_on`, waits for. `quickstart/tests/test_e2e.py` runs sign-in and permission checks against the running stack's published ports (skipped when it's down).
- A Windows WSL kernel missing Podman's nftables bridge support can prevent container startup even though builds and Compose validation pass (observed with Podman 6.0.2 / WSL kernel 6.6.87.2). The quickstart README documents the upstream issue and WSL update/restart path. Do not work around it by globally disabling firewall rules or collapsing all services into one namespace.
- Quickstart-only `start.py` supervises the normal programs and relays canonical localhost OAuth/API ports to the corresponding Compose services. `machine.py` approves its own two device grants through the issuer's CSRF-bound form, saves separate standard OAuth credential files, removes the bootstrap password from the launched environment, and runs `agent-machine`. It sets a non-loopback machine address so provider traffic uses fleet tunnels. It does not weaken API/issuer authentication. Its initial backends are empty; adopting machine-1's Ollama creates the default `machine-1` backend.

## Scripts (`.sh`/`.ps1` pairs; change both)

- `build-client`, `build-machine` (into `client/bin`, `machine/bin`), `build-dist` (wraps `tools/dist`; `PACKAGES=1` / `-Packages`), `agent` (build and run), `dev` (API with reload + Vite).
- No pair: `start-ollama.sh`, and `scripts/https/setup-https.ps1` (Windows: nginx + win-acme for Let's Encrypt, filling in `nginx.conf.template` and `nginx-https.conf.template`: the brochure site on the apex/`www` (`-SiteBackend`, :8080), the console on its hostname (`-ConsoleBackend`, :8081), the issuer on `auth.<domain>` (`-AuthBackend`, :10000) and API traffic on `api.<domain>` (`-Backend`, :9999)).
- `scripts/deploy.sh`/`.ps1` and the `deploy-remote.sh` both send (gitignored, so they exist only on the developer's computer): release to the server's own computer. From a clean `master` (optionally setting and tagging `VERSION` first) it pushes, runs `build-dist`, has the remote checkout `git pull --ff-only` to the same commit and copies `dist/<version>/` there. Then `deploy-remote.sh` runs there (filled in and base64-encoded, since PowerShell 5.1 mangles double quotes in native arguments; the sudo password on stdin): it checks SHA256SUMS, moves an `AI_PROXY_VERSION` pin in the unit's `version.conf` drop-in to the new build (a pin otherwise keeps the old version reported and handed out), restarts the unit, waits for `/health`, and rebuilds whichever of the Compose services in `AI_PROXY_DEPLOY_CONTAINERS` (default `console marketing-site`) are running, the console with `VITE_API_URL` = `AI_PROXY_DEPLOY_CONSOLE_API`, since the split-hosted console is a container that a server restart doesn't change. The deploy fails if `/health` doesn't report the new version or a rebuild fails. The server works its version out from its checkout, so build and checkout must be the same clean commit. Settings: `AI_PROXY_DEPLOY_TARGET`, `_DIR`, `_SERVICE`, `_HEALTH`, `_PASSWORD`, `_CONTAINERS`, `_CONSOLE_API`.
- `scripts/screenshots/`: `demo.py` (a self-contained demo server on :8199 with fake machines and backends), `traffic.py` (drives agent traffic through it) and `capture.mjs` (Playwright screenshots into `docs/screenshots/`).

## Rules

- Keep `go.mod` at Go 1.24 and build with `GOTOOLCHAIN=local`.
- Don't commit `dist/`, `ui/dist`, `logs/`, `certs/` or the `bin/` folders.
- Linux package versions are `X.Y.Z-<release>`. `PackageRelease` makes the release the commits since `vX.Y.Z` plus one, so each untagged build upgrades the one before. Keep the release rising if the version scheme changes.
