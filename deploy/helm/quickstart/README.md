# Helm quickstart

The [Helm chart](../agent-mesh/README.md) on a local Kubernetes cluster (kind, k3d, minikube, or
Docker Desktop's or Podman Desktop's), from the published images (`latest`). The step-by-step
guide is [docs/quickstart-kubernetes.md](../../../docs/quickstart-kubernetes.md). From the repository root:

```bash
helm install mesh deploy/helm/agent-mesh -n mesh --create-namespace -f deploy/helm/quickstart/values.yaml
kubectl -n mesh apply -f deploy/helm/quickstart/machine.yaml
```

| File | What it is |
| --- | --- |
| [`values.yaml`](values.yaml) | The chart's smallest form: one pod with every role, SQLite on a volume, agent-auth in the same pod, plain HTTP on `localhost` through a port-forward. |
| [`machine.yaml`](machine.yaml) | A fleet machine: the `agent-sandbox` image and Ollama in one pod. It assumes the release is called `mesh`. |

It's a demo, like the Compose quickstart. For real use, start from the chart's
`values-single.yaml` or `values-scaled.yaml` ([Running several server processes](../../../docs/scaling.md)).
