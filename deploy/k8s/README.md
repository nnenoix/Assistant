# Kubernetes deployment (Phase 0)

Minimal, single-replica manifests for running the Workspace Agent on
Kubernetes. Designed for a small dev / staging cluster — three pods
(agent, postgres, redis) on one node. Not yet wired for HA / autoscaling
/ multi-region — those come in a Helm-chart followup.

## Layout

```
deploy/k8s/
├── README.md                # this file
├── 00-namespace.yaml        # workspace-agent ns
├── 10-configmap.yaml        # non-secret env (OIDC issuer, log level, …)
├── 11-secret.example.yaml   # secrets template — DO NOT commit real values
├── 20-postgres.yaml         # StatefulSet + PVC + headless Service
├── 21-redis.yaml            # Deployment + Service (no persistence — arq queue is ephemeral)
├── 30-agent.yaml            # Deployment + Service for the agent
└── 40-ingress.yaml          # TLS-terminated ingress (nginx-ingress)
```

## Apply order

The numeric prefixes give safe apply order — `kubectl apply -f deploy/k8s/`
walks them in lexical sort. Equivalent for `--server-side`:

```bash
kubectl create -f deploy/k8s/11-secret.example.yaml  # edit first!
kubectl apply -f deploy/k8s/
```

## Prerequisites

- A Kubernetes cluster (kind, k3d, EKS, GKE, anything ≥ 1.27)
- An ingress controller (nginx-ingress used in `40-ingress.yaml`)
- cert-manager + a ClusterIssuer if you want auto-TLS
- An OIDC IdP reachable from the cluster (set `OIDC_ISSUER` in the
  ConfigMap; default points at an in-cluster Authentik)

## Secrets

`11-secret.example.yaml` is a **template** — never commit a populated copy.
Replace the base64-placeholder values:

```bash
echo -n 'super-secret-pg-password' | base64
echo -n 'a-very-long-authentik-key' | base64
```

…or generate via:

```bash
kubectl create secret generic workspace-agent-secrets \
    --from-literal=POSTGRES_PASSWORD=$(openssl rand -base64 32) \
    --from-literal=AUTHENTIK_SECRET_KEY=$(openssl rand -base64 64) \
    --from-literal=OIDC_CLIENT_SECRET=$(openssl rand -base64 32) \
    -n workspace-agent
```

## Image

The `30-agent.yaml` deployment references `workspace-agent:latest`. Push
your build to a registry the cluster can pull from:

```bash
docker build -t myregistry.io/workspace-agent:latest .
docker push myregistry.io/workspace-agent:latest
# Then patch the manifest:
kubectl set image deployment/workspace-agent agent=myregistry.io/workspace-agent:latest -n workspace-agent
```

## Verify

```bash
kubectl -n workspace-agent get pods
# NAME                                READY   STATUS    RESTARTS   AGE
# postgres-0                          1/1     Running   0          1m
# redis-…                             1/1     Running   0          1m
# workspace-agent-…                   1/1     Running   0          30s

kubectl -n workspace-agent port-forward svc/workspace-agent 8765:8765
curl http://localhost:8765/health    # {"status":"ok"}
curl http://localhost:8765/metrics   # Prometheus exposition
```

## Followups (NOT in this minimal manifest set)

- Helm chart with values.yaml + templates/
- HorizontalPodAutoscaler on the agent Deployment
- PodDisruptionBudget for the StatefulSet
- ServiceMonitor CR (prom-operator) instead of static scrape config
- NetworkPolicy locking down pg/redis to agent pods only
- Externally-managed Postgres (CloudSQL / Aurora) + ExternalSecrets
- Multi-AZ / multi-replica scaling
