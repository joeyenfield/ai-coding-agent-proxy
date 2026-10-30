# Local container quickstart

From this folder, with Podman and a Compose provider:

```bash
podman compose up -d --build
```

On Windows and macOS, start your Podman machine first with `podman machine start`
if it is stopped. If no machine exists yet, run `podman machine init` once first.
`podman compose version` checks that a Compose provider is installed; Podman Desktop
can install one through **Settings > Resources > Compose**. The provider may be named
Docker Compose; it runs against the Podman engine through `podman compose`.

Docker Compose with Linux containers also works:

```bash
docker compose up -d --build
```

The examples below use `podman compose`; substitute `docker compose` when using Docker.

Open **http://localhost:19999** and sign in with **admin** / **secret**. Use `localhost`,
so the browser matches the registered callback. The first build downloads the server,
console, Go clients, coding agents and Ollama; it can take several minutes.

The stack has:

| Service | Purpose |
| --- | --- |
| `server` | API and console on `127.0.0.1:19999`. |
| `auth` | Separate OIDC issuer on `127.0.0.1:20000`. |
| `machine-1` | Fleet machine with the launcher, coding agents and `/workspace`. |
| `machine-2` | Another independent fleet machine with its own workspace and sign-ins. |
| `ollama` | Ollama, relayed to machine-1's loopback; models persist in a volume. |

Both machines sign in automatically as admin using separate OAuth device approvals for
`agent` and `agent-machine`. Later starts reuse rotating credentials. Launches and shells
are enabled in `/workspace`; host folders and the Docker socket are not mounted.
Ollama runs on CPU by default and starts with no models downloaded.

This is a local demo with known credentials and an environment-defined admin. To choose
another password, set `QUICKSTART_ADMIN_PASSWORD` before starting the stack (or in a local
`.env` file). The environment account's password is managed by that variable. For a public
deployment, follow [OIDC deployment](../docs/oidc.md) with HTTPS and private secrets.

## Use Ollama

1. Open **Machines**, select **machine-1**, and choose **Add as a provider** for its Ollama
   server. It creates the `machine-1` backend, matching the quickstart's default backend.
2. Pull a model from that machine's model page. Model downloads can take a while and
   consume several GB, depending on the model you choose.
3. Start an agent on either machine, choose the Ollama backend and your downloaded model,
   and use `/workspace` as its project directory.

The provider belongs to machine-1. Agent traffic from either machine goes through the
server and the fleet tunnel to it. Only machine-1 discovers Ollama: its helper relays `127.0.0.1:11434` to the
`ollama` container. Its process lifecycle
is controlled by Compose; use the console for its model operations.

You can also open a terminal or run the launcher in either machine:

```bash
podman compose exec machine-2 agent --list
podman compose exec machine-2 sh
```

## End-to-end tests

With the stack running, from the repository root:

```bash
python -m pytest -q quickstart/tests
```

They sign in through the real issuer as a browser and as `agent login` would, and check
refused passwords and origins, admin-only routes, machine sharing through organization
grants, and that removing a user ends their sign-in. They create and remove their own
user and organization. The issuer allows 10 password attempts per address per 10 minutes
and the suite makes 7: run `podman compose restart auth` before a quick rerun.

## Inspect, stop and reset

```bash
podman compose ps
podman compose logs -f machine-1 machine-2
podman compose logs -f server auth
podman compose stop
podman compose up -d
```

Configuration, recordings, auth state, both machines' home folders and workspaces, and
Ollama models have separate named volumes. They survive container recreation. This stack
uses its own Compose project and does not read your checkout's runtime configuration.

To delete **all quickstart state, workspaces and downloaded models** and start fresh:

```bash
podman compose down -v
podman compose up -d --build
```

If you change the password after signing machines in, reset this demo or sign both
programs in again; password changes invalidate their existing OAuth credentials.

## How local authentication works

The canonical issuer is `http://localhost:20000`. A small TCP forwarder in the server
and each machine makes that same address reach `auth` from inside containers. Each
machine also forwards `localhost:19999` to the API. The browser uses the published
loopback ports. This keeps the normal loopback-development OAuth validation and the
components' separate network namespaces. The helper scripts only provision this demo;
the server and issuer retain their regular authentication and permission checks.

Image references include their registry so Podman never needs a short-name selection
prompt. Helper files use shared SELinux labels (`:ro,z`) because multiple containers
read them. Every service has its own network namespace. Do not share one between services
(`network_mode: service:...`) or put them in a shared Podman pod: Podman then refuses to
recreate a container after a rebuild (`has dependent containers`).

## Build context permission errors

If Compose reports `checking context: can't stat` for a generated screenshot or test
folder, use the checkout's current root `.dockerignore`. It excludes `.screenshot*`,
`.test-*`, `.tmp-*`, virtual environments and local agent history before Compose scans
the context. You do not need to delete those folders or change their Windows permissions.

## Podman on Windows: localhost:19999 doesn't answer

Use a rootless Podman machine (Podman's default). A rootful WSL machine publishes ports
with nftables rules only and opens no socket, and WSL only forwards ports with a listening
socket to Windows, so the browser can't reach the stack. Switch it with
`podman machine stop`, `podman machine set --rootful=false` and `podman machine start`,
then `podman compose up -d --build` again (rootless has its own images and volumes).

If ports still refuse right after switching (`No route to host` from inside the VM), the
rootful rules are still loaded: all WSL distributions share one kernel. Run `wsl --shutdown`
(it stops **all** distributions) and start the machine again.

`podman machine start` copies the shell's `HTTP_PROXY`/`HTTPS_PROXY` into the VM. Start it
from a shell without them if pulls fail with `proxyconnect ... connection refused`.

## Podman on Windows: nftables startup errors

If containers fail before startup with `netavark ... nftables error: "nft" did not
return successfully while applying ruleset`, check the WSL kernel. Podman has a
[reported WSL compatibility issue](https://github.com/containers/netavark/issues/1495)
with this error, including Podman 6.0.2 and kernel `6.6.87.2-microsoft-standard-WSL2`.
The same failure can affect rootless containers on a Compose bridge network.

Update WSL with `wsl --update`. Once you have saved other WSL work, stop the Podman
machine, run `wsl --shutdown` (which stops **all** WSL distributions), then start the
Podman machine again and retry. See Microsoft's [WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands).
If Windows or organization policy prevents updating WSL, use a Podman VM/kernel that
supports its nftables bridge networking. This quickstart does not disable the firewall
or change the VM's global network configuration to work around the error.
