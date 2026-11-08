# Deploy

Everything here runs the **published images** from the GitHub Container Registry
(`ghcr.io/joeyenfield/agent-server`, `agent-sandbox`, `ai-control-mesh` and the site), built by
[`.github/workflows/images.yml`](../.github/workflows/images.yml). Nothing here builds this
checkout's code; for that, see [`devhelpers/`](../devhelpers/README.md).

`AI_PROXY_IMAGE_TAG` picks the version: `latest` (the default, the newest build of master), a
release such as `2.1.54`, or `sha-<commit>` for one commit of master. `AI_PROXY_IMAGES` points at
another registry or owner, such as a fork's `ghcr.io/<you>`.

## Which one

| You want | Use | Guide |
| --- | --- | --- |
| To try it on one computer, in one container | [`compose/allinone/`](compose/allinone/compose.yaml) | [Run it on your own computer](../docs/local.md) |
| To try it on one computer: a server, two machines and a local model | [`compose/quickstart/`](compose/quickstart/README.md) | [Quickstart with Docker or Podman](../docs/quickstart.md) |
| To try two linked servers (peering) | [`compose/peer/`](compose/peer/README.md) | [Peering](../docs/peering.md#try-it) |
| A server for real use with Docker Compose | [`compose/server/`](compose/server/compose.yaml) | [Deployment](../docs/deployment.md) |
| To try the Helm chart on a local Kubernetes cluster | [`helm/quickstart/`](helm/quickstart/README.md) | [Quickstart on Kubernetes](../docs/quickstart-kubernetes.md) |
| Kubernetes for real use | [`helm/agent-mesh/`](helm/agent-mesh/README.md), the chart | [Running several server processes](../docs/scaling.md) |

[`compose/README.md`](compose/README.md) has more on the Compose folders.

## Layout

```text
deploy/
  compose/
    allinone/     the whole system in one container
    quickstart/   PostgreSQL, the server, its issuer, two machines and Ollama (start.py, machine.py: shared helpers)
    peer/         two linked servers with a machine each, and setup.py to link and bridge them
    server/       a server for real use: PostgreSQL, agent-auth, agent-server, the site
  helm/
    agent-mesh/   the Helm chart
    quickstart/   values and a machine for the chart on a local cluster
```
