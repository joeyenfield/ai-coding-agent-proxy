# Developer helpers

For working on AI Control Mesh: stacks that **build this checkout's code**, and the scripts for
everyday development, builds, screenshots, releases and deploys. To run the published images
instead, use [`deploy/`](../deploy/README.md).

Each stack here is the `deploy/compose/` stack of the same name, built from source: the same
services, settings and ports (`server/tests/test_quickstart.py` keeps them in step). Their images
get local names (`localhost/ai-control-mesh-dev-*`) and their own project names, so a development
build never replaces a published image or opens the deploy stack's volumes. They use the same
host ports as the deploy stacks, so stop one before starting the other.

## Stacks

From the folder:

| Folder | Run | What it is |
| --- | --- | --- |
| [`basic/`](basic/compose.yaml) | `podman compose up -d --build` | The quickstart: PostgreSQL, the server, its issuer, two machines and Ollama, on http://localhost:19999 (admin / secret). [`basic/tests/test_e2e.py`](basic/tests/test_e2e.py) checks a running one. |
| [`allinone/`](allinone/compose.yaml) | `podman compose up -d --build` | The `ai-control-mesh` image, from its [`Dockerfile`](allinone/Dockerfile) here (also what CI publishes). |
| [`peer/`](peer/compose.yaml) | `podman compose up -d --build`, then `python3 ../../deploy/compose/peer/setup.py` | Two linked servers with a machine each ([Peering](../docs/peering.md)). |
| [`testdata/`](testdata/compose.yaml) | `podman compose up -d --build` | Development only (no `deploy/` twin): a server full of test data on http://localhost:49999 (admin / secret). Three organizations (Acme Labs, Globex, Initech) with nine people in teams, who sign in with `testdata`, and a machine per organization signed in as its admin. Each machine has its own Ollama with a small model (`TESTDATA_MODEL`, default `qwen3:0.6b`; empty for none), shares the [toolbox MCP server](testdata/mcp_toolbox.py), and lets its whole organization see it, start agents there and use its MCP servers. Its ports differ from `basic/`'s, so both can run. The header of its `compose.yaml` lists who's who. |
| [`server/`](server/compose.build.yaml) | From the root: `docker compose -f deploy/compose/server/compose.yaml -f devhelpers/server/compose.build.yaml up -d --build` | Builds `deploy/compose/server`'s images instead of pulling them. |

`--build` rebuilds after a change; the first build takes several minutes. The images say
`<VERSION>+unknown`, as the build has no git. `podman compose down -v` removes a stack and its
volumes.

The basic stack provisions its vault encryption key automatically on first server start, at `/app/logs/private/vault-keys` in its persistent `server-logs` volume. It reuses that key on restart, so provider API keys entered in the console remain readable. You can supply `AI_PROXY_VAULT_KEYS` through the environment or a `.env` file instead. This is the encryption key, separate from a provider's API token. After updating an existing basic stack, run `podman compose up -d --force-recreate server` from `devhelpers/basic` to enable provisioning. Keep the key when backing up or restoring the database; removing the logs volume loses the generated key.

## Scripts

[`scripts/`](scripts) holds the scripts, each as a `.sh` and `.ps1` pair (run from anywhere; they
find the repository themselves):

| Script | What it does |
| --- | --- |
| `dev.sh`, `dev.ps1` | The API with auto-reload and the Vite dev server on :5173. It uses the server's port 9999, so not on a computer whose real server uses it. |
| `agent.sh`, `agent.ps1` | Builds the client and runs it, for trying launcher changes. |
| `build-client`, `build-machine` | `agent` and `agent-proxy` into `client/bin`, `agent-machine` into `machine/bin`. |
| `build-dist` | Every platform's archives into `dist/<version>/` (`PACKAGES=1` / `-Packages` adds .deb, .rpm, .apk and Arch packages), which the server hands out. |
| `install-local` | Installs and starts `agent-server local` for one person, from this checkout ([Run it on your own computer](../docs/local.md)). |
| `screenshots/demo.py` | A self-contained demo server with made-up data on http://127.0.0.1:8199 for checking the console: sign in as demo-admin / demo-admin-screenshot-password (a site admin) or demo / demo-screenshot-password. `--capture [names]` takes `docs/screenshots/` and stops; `--stop` stops one left running. |
| `screenshots/` | Also `capture.mjs` (the console's screenshots), `quickstart-steps.mjs` (the quickstart guide's), `site-images.py` and `traffic.py`. |
| `site/` | Checks (`check.mjs`), previews and fingerprints the brochure site. |
| `release/release.py` | A GitHub Release's files and web installers; `release.yml` runs it on `v*` tags. |
| `deploy.sh`, `deploy.ps1` | Deploys master (or a new version) to the project's server: pushes, waits for the images, and restarts its Compose stack (`deploy-remote.sh` runs there). |
| `https/` | nginx with Let's Encrypt in front of a server (`setup-https.ps1`). |
| `start-ollama.sh` | Starts Ollama listening on every address. |

[AGENTS.md](../AGENTS.md) has the commands for tests and builds, and [docs/development.md](../docs/development.md) the rest.
