# agent-mesh Helm chart

This chart runs AI Control Mesh on Kubernetes: `agent-server` (the API, the model gateway, HTTPS capture, the fleet and terminals), `agent-auth` (its OIDC issuer), and optionally the standalone console. Both programs come from one image, `ghcr.io/joeyenfield/agent-server`. The chart's `appVersion` is the repository's `VERSION`, and it is the image tag unless you set `image.tag`.

For installs without Kubernetes, use Compose instead (`deploy/compose/`). A one-computer quickstart on a local cluster is in `deploy/helm/quickstart/`. The design behind the chart is in `docs/scaling-internals.md`, and running it in `docs/scaling.md`.

## Modes

| | `mode: single` | `mode: scaled` |
| --- | --- | --- |
| Pods | One Deployment, `replicas: 1`, `strategy: Recreate`, `AI_PROXY_ROLES=all` | One Deployment per entry under `roles`, each with its own Service, HPA and PDB |
| Database | SQLite on a PersistentVolumeClaim (`persistence`), or PostgreSQL | PostgreSQL: external or CloudNativePG |
| Redis | Optional (needed only for `single.workers` > 1) | Required: external or bundled Valkey |
| agent-auth | In the server's pod with SQLite (it reads the API's database file); its own Deployment with PostgreSQL | Its own Deployment, 2+ replicas, JWT access tokens on |
| Routing | Everything on `server.host` goes to one Service | §5's path table, each path to its role's Service |

The roles are `api`, `gateway`, `intercept`, `mcp`, `fleet`, `realtime` and `worker`. By default the gateway also runs `mcp`. Every role must be served by exactly one enabled entry, and the chart refuses to render otherwise. Entries can group roles with `serves`, which gives the proposal's middle size of two Deployments (see `ci/scaled-middle-size-values.yaml`):

```yaml
roles:
  api: {enabled: false}        # ... and every other built-in entry
  control: {enabled: true, serves: [api, realtime, worker], replicas: 2}
  edge:    {enabled: true, serves: [gateway, mcp, intercept, fleet], replicas: 3, terminationGracePeriodSeconds: 300}
```

## Example values

| File | What it is |
| --- | --- |
| `values-single.yaml` | One pod, SQLite on a volume, the console served by the server, ingress-nginx with cert-manager |
| `values-scaled.yaml` | A Deployment per role on PostgreSQL and Redis you already run, the standalone console, Gateway API |
| `values-cloud.yaml` | Managed services (RDS behind RDS Proxy, ElastiCache, S3 through IRSA), secrets from AWS Secrets Manager through External Secrets |
| `values-onprem.yaml` | CloudNativePG with a PgBouncer Pooler, the bundled Valkey, an S3-compatible store, ingress-nginx |

## Before you install

1. **DNS and TLS** for `server.host` (the API, for example `api.example.com`) and `auth.host` (the issuer, for example `auth.example.com`). Add `console.host` too if you deploy the standalone console. Keep them on HTTPS subdomains of one site: the sign-in cookies are host-only and `SameSite=Lax` (see `docs/oidc.md`).
2. **Routing**: either a Gateway API `Gateway` with listeners for those hosts (`routing.gatewayAPI.gatewayRef`), or an Ingress controller (`routing.ingress`).
3. **Secrets** (below). The chart never takes database or Redis URLs in values.
4. For `mode: scaled`: PostgreSQL and Redis, or the CloudNativePG operator for `postgresql.cloudnativepg` (the chart renders the `Cluster` and `Pooler`, not the operator).
5. Optional: the Prometheus Operator (`monitoring.*`), the External Secrets Operator (`secrets.externalSecrets`), and the Prometheus adapter for custom-metric autoscaling.

### Secrets

| Secret | Keys | Used for |
| --- | --- | --- |
| `secrets.existingSecret`, else `<release>-agent-mesh-app` (generated) | `internal-token`, `oidc-introspection-secret`, `vault-keys` | `AI_PROXY_INTERNAL_TOKEN` (pod to pod), `AI_PROXY_OIDC_INTROSPECTION_SECRET` (agent-server and agent-auth), `AI_PROXY_VAULT_KEYS` (optional; without it subscriptions' managed tokens can't be stored) |
| `postgresql.external.existingSecret` | `url`, `auth-url`, optionally `migration-url` | `AI_PROXY_DATABASE_URL` (role `mesh_api`), `AI_PROXY_AUTH_DATABASE_URL` (role `mesh_auth`), the migration Job's direct URL |
| `redis.external.existingSecret` | `url` | `AI_PROXY_REDIS_URL` (`redis://` or `rediss://`) |
| `objectStorage.existingSecret` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Object storage, unless you use web identity |
| `auth.bootstrapAdmin.existingSecret` | `username`, `password` | Optional: an admin outside the users table (`AI_PROXY_USERNAME`/`AI_PROXY_PASSWORD`) |

If you don't name an app Secret, the chart generates the internal token and the introspection secret once. It keeps them across upgrades (it reads the Secret already in the cluster) and leaves the Secret in place on uninstall (`helm.sh/resource-policy: keep`). The same applies to the CloudNativePG role passwords and the bundled Valkey's password.

```bash
kubectl create namespace mesh
kubectl -n mesh create secret generic mesh-app \
  --from-literal=internal-token="$(openssl rand -hex 32)" \
  --from-literal=oidc-introspection-secret="$(openssl rand -hex 32)" \
  --from-literal=vault-keys="$(agent-server vault key)"
kubectl -n mesh create secret generic mesh-postgres \
  --from-literal=url='postgresql+psycopg://mesh_api:<password>@db.example.com:5432/mesh?sslmode=require' \
  --from-literal=auth-url='postgresql+psycopg://mesh_auth:<password>@db.example.com:5432/mesh?sslmode=require' \
  --from-literal=migration-url='postgresql+psycopg://mesh_api:<password>@db-direct.example.com:5432/mesh?sslmode=require'
kubectl -n mesh create secret generic mesh-redis --from-literal=url='rediss://:<token>@redis.example.com:6379/0'
```

`agent-server vault key` prints a new vault key on any computer where the server is installed. Its format is the server's (see `docs/subscriptions.md`).

With External Secrets (`secrets.externalSecrets.enabled`), the app Secret is an `ExternalSecret` mapped through `secrets.externalSecrets.app`. Add the database, Redis and object-storage Secrets under `extra`, named as the `existingSecret` values refer to them (`values-cloud.yaml` shows all of it).

### An external PostgreSQL

Run what `server/docker/postgres-init.sh` runs, as the database's administrator, once:

```sql
CREATE ROLE mesh_api LOGIN PASSWORD '<api password>';
CREATE ROLE mesh_auth LOGIN PASSWORD '<auth password>';
CREATE SCHEMA mesh AUTHORIZATION mesh_api;
CREATE SCHEMA mesh_auth AUTHORIZATION mesh_auth;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE mesh FROM PUBLIC;
GRANT CONNECT ON DATABASE mesh TO mesh_api, mesh_auth;
```

On managed services, connect as the service's admin role and grant it membership of `mesh_api` and `mesh_auth` first if `AUTHORIZATION` requires it (RDS, Cloud SQL). The server's own migration grants `mesh_auth` read access to the account tables it needs.

If you put a transaction-mode pooler (RDS Proxy, PgBouncer) in front, point `migration-url` at the database itself: the migration holds an advisory lock for its whole run.

## Install

### Single, small team

```bash
helm install mesh deploy/helm/agent-mesh -n mesh --create-namespace \
  -f deploy/helm/agent-mesh/values-single.yaml \
  --set server.host=api.example.com --set auth.host=auth.example.com
```

The server migrates its SQLite database itself (`AI_PROXY_MIGRATE=1`), and agent-auth runs in the same pod. On the first start the server creates `admin` and prints its password in the log (NOTES.txt shows the command). `persistence` keeps the database, the server's log, `certs/first-user-password` and agent-auth's state. Back that volume up, or move to PostgreSQL, which also works in `mode: single`.

### Scaled, managed cloud

1. Create the database (above), Redis, a bucket, and an IAM role the ServiceAccount can assume. With IRSA, annotate `objectStorage.serviceAccountAnnotations` (`eks.amazonaws.com/role-arn`). For GKE Workload Identity use `iam.gke.io/gcp-service-account`; for Azure Workload Identity use `azure.workload.identity/client-id`, with the pod label through `roles.<name>.podLabels`.
2. Build the console image if you want it standalone: `docker build -f ui/Dockerfile --build-arg VITE_API_URL=https://api.example.com -t <registry>/console:<version> .`, then push it. It isn't published, because it carries its API's address.
3. Install:

```bash
helm install mesh deploy/helm/agent-mesh -n mesh --create-namespace -f deploy/helm/agent-mesh/values-cloud.yaml
```

### Scaled, on-prem

1. Install the CloudNativePG operator, plus Gateway API CRDs and an implementation, or ingress-nginx.
2. Create the S3-compatible bucket and its key Secret (`objectStorage.existingSecret`), and the app Secret.
3. Install:

```bash
helm install mesh deploy/helm/agent-mesh -n mesh --create-namespace -f deploy/helm/agent-mesh/values-onprem.yaml
```

The chart creates database `mesh`, roles `mesh_api` (owner) and `mesh_auth`, and schemas `mesh` and `mesh_auth`, as `server/docker/postgres-init.sh` does. `mesh_auth` is made in `postInitApplicationSQL` and then given its password and `LOGIN` by `spec.managed.roles`. The passwords are generated into `kubernetes.io/basic-auth` Secrets, or you can name your own with `existingApiSecret` and `existingAuthSecret`. The `Cluster` is kept on uninstall. Set `postgresql.cloudnativepg.backup`.

The bundled Valkey is **one instance with AOF persistence, not Sentinel**. `AI_PROXY_REDIS_URL` is a plain `redis://` URL. If Valkey restarts, presence and pub/sub are gone for a few seconds, and machines and terminal viewers reconnect. For high availability, run a managed Redis or an operator's and use `redis.external`.

## Migrations

Server pods run with `AI_PROXY_MIGRATE=0` and wait for the schema. A Job runs `agent-server db migrate`, retrying every 10 s (`migrations.attempts`) while the database comes up:

- **Upgrades:** always a `pre-upgrade` hook.
- **Install with an external database:** a `pre-install` hook, so the schema exists before any pod starts.
- **Install with CloudNativePG or External Secrets:** a plain Job created with the release (`<release>-agent-mesh-migrate-install`), because a pre-install hook would run before the cluster or the Secrets exist. Helm deletes it on the next upgrade. `migrations.onInstall` (`auto`, `hook`, `job`) overrides the choice.

The Job always connects directly, never through PgBouncer. agent-auth migrates its own schema when it starts. `retention` is passed to the Job as `AI_PROXY_RETENTION_SEED` (JSON), and it only seeds the defaults on the first install.

## Routing

The table is in `templates/_helpers.tpl` (`agent-mesh.routeTable`) and is shared by HTTPRoutes and Ingresses.

| Path on `server.host` | Role |
| --- | --- |
| `/session/*` (agents' `/v1`, `/anthropic`, `/api` and MCP), `/v1/*`, `/anthropic/*`, bare `/api/{chat,generate,embed,embeddings,tags,show,ps,version}` | gateway |
| `^/session/[^/]+/mcp(/.*)?$` (only when another Deployment serves `mcp`) | mcp |
| `/api/proxy/tunnel` | intercept |
| `/api/machines/connect`, `/api/machines/tunnel/*` | fleet |
| `/api/terminals/*`, `/api/live*`, `/api/remote*` | realtime |
| everything else (`/api/*`, `/openapi.json`, `/docs`, `/downloads/*`, `/install.*`, `/help-files/*`, `/health`, the console) | api |
| `auth.host` | agent-auth |
| `console.host` | console |

Every path except the api role's gets long-stream settings:

- **Gateway API:** `rules[].timeouts` from `routing.gatewayAPI.streamingTimeouts` (default `request: 0s`, no timeout). Some implementations take idle timeouts from their own policies instead (Envoy Gateway's `BackendTrafficPolicy` and `ClientTrafficPolicy`). Set `streamingTimeouts: {}` if yours rejects the field.
- **Ingress:** the long paths get their own Ingress with `routing.ingress.streamingAnnotations`. The defaults are for ingress-nginx: 3600 s read and send timeouts, buffering off. ingress-nginx passes WebSockets through without extra settings. Other controllers need their own equivalents. Keep the read timeout above the fleet's and terminals' ping interval, and the body size above the largest request agents send (whole conversations, several hundred KB).
- **A separate `mcp` Deployment needs a regular-expression match.** Gateway API's `RegularExpression` is implementation-specific, including its precedence against the `/session` prefix, so check that yours sends `/session/<id>/mcp/...` to the mcp Service. With ingress-nginx, the chart renders the path on an Ingress with `use-regex`, which makes every path of that host a regular expression. Leaving `mcp` in the gateway (the default) avoids both.

The internal port (`server.internalPort`, 9100) is never routed.

## Autoscaling

Every role's HPA scales on CPU by default (`roles.<name>.autoscaling`). Connection-bound roles scale better on connections. Set `metric` to a name the Prometheus adapter serves as a Pods metric, and `target` to the average per pod:

```yaml
roles:
  gateway: {autoscaling: {enabled: true, min: 3, max: 20, metric: open_streams, target: 500}}
  fleet:   {autoscaling: {enabled: true, min: 3, max: 8, metric: fleet_sockets, target: 1000}}
```

`autoscaling.metricNames` maps `open_streams` to `agent_server_open_streams` and `fleet_sockets` to `agent_server_fleet_sockets`; any other value becomes `agent_server_<metric>`. A prometheus-adapter rule that serves them:

```yaml
rules:
  - seriesQuery: '{__name__=~"agent_server_(open_streams|fleet_sockets)",namespace!="",pod!=""}'
    resources: {overrides: {namespace: {resource: namespace}, pod: {resource: pod}}}
    name: {matches: "^(.*)$", as: "${1}"}
    metricsQuery: 'sum(<<.Series>>{<<.LabelMatchers>>}) by (<<.GroupBy>>)'
```

Scale-in is slow by default: one pod every 2 minutes after a 10-minute window, because each pod that drains sends its machines and viewers to reconnect elsewhere. Override this with `autoscaling.behavior`. The worker role has no HPA: it keeps 2 replicas and one of them is active.

## Shutdown and probes

- **Probes:** liveness `GET /health` and readiness `GET /ready` on the main port. `/ready` answers 503, with details, while the database, Redis or blob storage doesn't answer, and 200 otherwise. With `AI_PROXY_MIGRATE=0` a pod waits up to 10 minutes for the migration Job's schema before it listens at all, so neither probe answers until then. The startup probe on `/health` allows those 10 minutes (`server.probes.startupFailureThreshold`) before liveness starts. agent-auth uses `GET /health` on its port.
- **Draining:** `preStop` sleeps `server.preStopSleepSeconds` (5 s) so endpoints are removed before SIGTERM starts the drain.
- **Grace periods** (`terminationGracePeriodSeconds`): gateway, intercept and mcp 300 s; fleet and realtime 120 s; the rest 30 s; `mode: single` 300 s. Each server container gets `AI_PROXY_GRACEFUL_SECONDS` = its grace period minus 15 s (at least 5 s), uvicorn's graceful shutdown timeout, so long streams drain rather than being cut after uvicorn's default 5 s.
- **PodDisruptionBudgets** (`maxUnavailable: 1`) cover every role with more than one replica.
- **Spread:** `topologySpread.constraints` spreads pods over zones, then nodes.

## Monitoring

`/metrics` is served without sign-in on every server pod's internal port (9100). With `AI_PROXY_WORKERS` > 1 (`single.workers`, `roles.<name>.workers`) each worker takes an ephemeral internal port instead, so `/metrics` isn't on 9100 in those pods, and the ServiceMonitor doesn't reach them. The network policy then lets the release's server pods reach each other on any TCP port. A headless Service, `<release>-agent-mesh-metrics`, lists every server pod. With `monitoring.serviceMonitor.enabled` a `ServiceMonitor` scrapes it and labels each series with its `role`. CloudNativePG pod monitors are turned on too. `monitoring.prometheusRule.enabled` adds these alerts:

- `AgentMeshServerDown`: a pod isn't scraped.
- `AgentMeshNoServerTargets`: no pod is scraped.
- `AgentMeshPodNotReady`: a pod has not been ready for 10 minutes (needs kube-state-metrics).
- `AgentMeshPodRestarting`: a container restarted more than 3 times in 30 minutes.
- `AgentMeshAuditBacklog`: `agent_server_audit_backlog` has been over `auditBacklogThreshold` for 15 minutes. This is a placeholder until the audit service exports it.

## Network policy

`networkPolicy.enabled` (on by default) restricts ingress only:

- **Server pods' main port:** open to anyone, or to `networkPolicy.publicFrom` (for example the ingress controller's namespace).
- **Internal port:** only this release's server pods and `networkPolicy.prometheusNamespace`. That's port-level, so Prometheus can reach every internal route. The server must require `AI_PROXY_INTERNAL_TOKEN` on everything there except `/metrics`.
- **agent-auth:** its port from the same peers as the main port, plus the server pods.
- **Bundled Valkey:** only this release's server and agent-auth pods.

Egress stays open, because the gateway reaches hosted model APIs and HTTPS capture reaches any public host.

## Values worth knowing

| Value | Default | Meaning |
| --- | --- | --- |
| `auth.issuer.forwardedAllowIps` | `*` | The same for agent-auth, whose sign-in rate limits count by the visitor's address. Set your ingress/gateway ranges, or restrict `networkPolicy.publicFrom`: with `*`, any pod that reaches it can pose as another address. |
| `server.forwardedAllowIps` | `127.0.0.1` | uvicorn's `FORWARDED_ALLOW_IPS`. Set trusted ingress/gateway addresses or CIDRs. Use `*` only when `networkPolicy.publicFrom` restricts access to trusted proxies. The default policy permits direct access to the public pod port. |
| `single.workers`, `roles.<name>.workers` | `1` | `AI_PROXY_WORKERS`. More than 1 needs Redis, and each worker then takes an ephemeral internal port: `/metrics` isn't on 9100 in those pods, and the network policy allows all TCP ports between the release's server pods. Keep `fleet` at 1. |
| `downloads.existingClaim` | empty | A ReadOnlyMany claim holding `dist/<version>/` for the Downloads page and `agent update` |
| `objectStorage.url` | `database` | `AI_PROXY_BLOB_URL`: `database`, `file:///app/logs/blobs` (single, on the volume), `s3://bucket/prefix` |
| `auth.issuer.jwtAccessTokens` | `auto` | `AI_PROXY_AUTH_JWT_ACCESS_TOKENS=1`, on in `mode: scaled` |
| `pgbouncer.directRoles` | `[worker]` | Roles that connect to the cluster directly (session-level locks, leader election) |
| `containerSecurityContext.readOnlyRootFilesystem` | `false` | The server writes only to mounted folders (`/app/logs`, `/app/certs`, `/app/auth-state`, `/tmp`). Turn it on once you've checked your version does too. |

Every value is commented in `values.yaml`.

## Upgrading and removing

- `helm upgrade` runs the migration hook first, then rolls each role (`maxUnavailable: 0`). `mode: single` stops its one pod before the new one starts.
- Switching from `mode: single` to `mode: scaled` needs PostgreSQL. Move SQLite data first with `agent-server db import` or `config export|import`.
- `helm uninstall` leaves these in place: the app Secret, the CloudNativePG role Secrets, the `Cluster`, and the single-mode volume. Delete them by hand to remove the data.

## Testing the chart

```bash
for f in deploy/helm/agent-mesh/ci/*.yaml deploy/helm/agent-mesh/values-*.yaml; do
  helm lint --strict deploy/helm/agent-mesh -f "$f"
  helm template mesh deploy/helm/agent-mesh -n mesh -f "$f" | kubeconform -strict -summary \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
done
```

Run it again with `--is-upgrade` to render the upgrade form of the migration Job. The `ci/` files cover:

- `mode: single` on SQLite, and on PostgreSQL with the console and an Ingress;
- `mode: scaled` on external services with custom-metric HPAs, a separate mcp Deployment and Gateway API;
- `mode: scaled` on CloudNativePG, Valkey and External Secrets with an Ingress;
- the two-group middle size with an external issuer.
