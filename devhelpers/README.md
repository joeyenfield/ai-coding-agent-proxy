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
| [`basic/`](basic/compose.yaml) | `podman compose up -d --build` | The quickstart: PostgreSQL, the server, its issuer, two machines and Ollama, on http://localhost:19999 (admin / secret). Unlike the deploy quickstart, it also has static test data: Company A (`compa.admin` and `compa.user`) and Company B (`compb.admin` and `compb.user`), who sign in with `testdata` (`TESTDATA_PASSWORD`). [`basic/tests/test_e2e.py`](basic/tests/test_e2e.py) checks a running one. |
| [`allinone/`](allinone/compose.yaml) | `podman compose up -d --build` | The `ai-control-mesh` image, from its [`Dockerfile`](allinone/Dockerfile) here (also what local releases publish). |
| [`peer/`](peer/compose.yaml) | `podman compose up -d --build`, then `python3 ../../deploy/compose/peer/setup.py` | Two linked servers with a machine each ([Peering](../docs/peering.md)). |
| [`testdata/`](testdata/compose.yaml) | `podman compose up -d --build` | Development only (no `deploy/` twin): a server full of test data on http://localhost:49999 (admin / secret). Three organizations (Acme Labs, Globex, Initech) with nine people in teams, who sign in with `testdata`, and a machine per organization signed in as its admin. Each machine has its own Ollama with a small model (`TESTDATA_MODEL`, default `qwen3:0.6b`; empty for none), shares the [toolbox MCP server](testdata/mcp_toolbox.py), and lets its whole organization see it, start agents there and use its MCP servers. Its ports differ from `basic/`'s, so both can run. The header of its `compose.yaml` lists who's who. |
| [`server/`](server/compose.build.yaml) | From the root: `docker compose -f deploy/compose/server/compose.yaml -f devhelpers/server/compose.build.yaml up -d --build` | Builds `deploy/compose/server`'s images instead of pulling them. |

`--build` rebuilds after a change; the first build takes several minutes. The images say
`<VERSION>+unknown`, as the build has no git. `podman compose down -v` removes a stack and its
volumes.

The basic stack provisions its vault encryption key automatically on first server start, at `/app/logs/private/vault-keys` in its persistent `server-logs` volume. It reuses that key on restart, so provider API keys entered in the console remain readable. You can supply `AI_PROXY_VAULT_KEYS` through the environment or a `.env` file instead. This is the encryption key, separate from a provider's API token. After updating an existing basic stack, run `podman compose up -d --force-recreate server` from `devhelpers/basic` to enable provisioning. Keep the key when backing up or restoring the database; removing the logs volume loses the generated key.

## Use a GPU

The stacks run their models on the CPU unless you add a GPU file. Both files hand over the
computer's NVIDIA GPUs by their CDI name, `nvidia.com/gpu=all`. They are separate files because
a container whose GPU isn't there won't start.

Podman or Docker must know the GPU by that name first. Check:

```bash
podman run --rm --device nvidia.com/gpu=all docker.io/library/ubuntu:24.04 nvidia-smi
```

If that prints your GPU, you're set. If not, [GPUs in containers](../docs/gpu.md) sets it up,
for Podman and Docker on Linux and Windows.

### One container

The all-in-one image leaves out Ollama's GPU libraries, as they are most of its size.
[`allinone/compose.gpu.yaml`](allinone/compose.gpu.yaml) builds an image that keeps the CUDA
ones and gives the container the GPUs. From `allinone/`:

```bash
podman compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

- **Use both files every time**, for `logs`, `exec` and `down` too.
- **The image is `localhost/ai-control-mesh-dev-gpu`**, 3.9 GB: the plain build's 2.8 GB plus
  the CUDA libraries. The plain build keeps its own name, so the two don't replace each other.
  Both use the same volumes.
- **`OLLAMA_GPU` picks the libraries** (default `cuda_v13`, for a recent driver). If Ollama
  still runs on the CPU, the driver is older: build with `OLLAMA_GPU=cuda_v12`, or
  `OLLAMA_GPU="cuda_v12 cuda_v13"` to keep both.
- **The published twin is `ghcr.io/joeyenfield/ai-control-mesh-gpu`**, which
  `deploy/compose/allinone/compose.gpu.yaml` runs: the same Dockerfile with `OLLAMA_GPU=cuda_v13`.

To check, start Ollama on the machine's page in the console, then:

```bash
podman compose -f compose.yaml -f compose.gpu.yaml exec mesh nvidia-smi -L
podman compose -f compose.yaml -f compose.gpu.yaml exec mesh ollama ps
```

The first names your card. The second shows `100% GPU` for a loaded model.

### The quickstart

`basic/` takes the quickstart's GPU file, which gives its `ollama` container the GPUs
([Use a GPU](../deploy/compose/quickstart/README.md#use-a-gpu)). From `basic/`:

```bash
podman compose -f compose.yaml -f ../../deploy/compose/quickstart/compose.gpu.yaml up -d --build
```

Docker needs the NVIDIA Container Toolkit with CDI turned on for the same files. This was
tested with Podman only.

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
| `https/` | nginx with Let's Encrypt in front of a server (`setup-https.ps1`). |
| `start-ollama.sh` | Starts Ollama listening on every address. |

[`release_scripts/`](release_scripts) holds the scripts for releasing and deploying:

| Script | What it does |
| --- | --- |
| `release.sh`, `release.ps1` | Local website, services or installer snapshots; explicit `--publish` uploads directly. No Actions runner. See [Local releases and snapshots](../docs/releases.md). |
| `release/release.py` | Packages already-built archives and the console into installer files. Called by the local release command. |
| `podman-arm64.sh`, `podman-arm64.ps1` | Gets Podman ready to build ARM64 images on an x64 computer: starts the Podman machine and registers its ARM64 emulator. Run it before a `--platform linux/arm64` release. |
| `deploy.sh`, `deploy.ps1` | Deploys master (or a new version) to the project's server: pushes, builds and publishes the ARM64 service image, and restarts its Compose stack (`deploy-remote.sh` runs there). Not in the repository. |

[AGENTS.md](../AGENTS.md) has the commands for tests and builds, and [docs/development.md](../docs/development.md) the rest.
