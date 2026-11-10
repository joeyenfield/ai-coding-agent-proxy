# Compose

Each folder here is a Compose project that runs the **published images** (`latest` unless
`AI_PROXY_IMAGE_TAG` names another). Run `podman compose up -d` (or `docker compose up -d`) from
the folder. To run this checkout's code instead, use the same stack in
[`devhelpers/`](../../devhelpers/README.md), which builds it from source.

| Folder | What it runs | Open |
| --- | --- | --- |
| [`allinone/`](allinone/compose.yaml) | The whole system in one container (the `ai-control-mesh` image): server, issuer, a machine of its own fleet with the coding agents and Ollama. The quickest start. | http://localhost:19999 |
| [`quickstart/`](quickstart/README.md) | PostgreSQL, the server, its OIDC issuer, two machines and Ollama, each in its own container. [docs/quickstart.md](../../docs/quickstart.md) walks through it. | http://localhost:19999 |
| [`peer/`](peer/README.md) | Two of the quickstart, linked: two servers with their issuers and databases, a machine on each, and a bridge between an organization on each ([docs/peering.md](../../docs/peering.md)). | http://localhost:29999 and http://127.0.0.1:39999 |
| [`server/`](server/compose.yaml) | A server for real use: PostgreSQL, agent-auth, agent-server and the brochure site, with a `sandbox` profile for a machine and a `console` profile for a separate console. Set its passwords in a `.env` beside it first ([Deployment](../../docs/deployment.md)). | http://&lt;this host&gt;:9999 |

`allinone/` and `quickstart/` use the same host ports, so run one of them at a time; `peer/` uses
its own. The quickstarts are demos: known passwords, ports on localhost only.

`quickstart/start.py` and `quickstart/machine.py` are shared: the containers' address relay and
the machines' device-flow sign-in. The `agent-server` and `agent-sandbox` images carry them in
`/quickstart` (with PostgreSQL's first-start script), so `quickstart/compose.yaml` mounts nothing
from the checkout and runs from a download of that one file. `peer/` and the other `devhelpers/`
stacks mount their own copy over them, and the all-in-one image copies `machine.py` in.
