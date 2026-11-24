# Deploying Concierge on k3s

A single-node k3s (or k3d on a laptop) is enough for the demo. Stateful dependencies come from their upstream charts;
Concierge's own chart deploys the gateway, the three backend services, the MCP server, the worker and the ingest Job.

```bash
# 1. cluster (k3d runs k3s in Docker)
k3d cluster create concierge -p "8080:80@loadbalancer"

# 2. dependencies
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add qdrant https://qdrant.github.io/qdrant-helm
helm install concierge-postgresql bitnami/postgresql \
  --set auth.username=concierge --set auth.database=concierge --set auth.existingSecret=concierge-postgresql \
  --set primary.initdb.scripts."init\.sql"="CREATE DATABASE inventory; CREATE DATABASE pricing; CREATE DATABASE reservations;"
helm install concierge-redis bitnami/redis --set architecture=standalone --set auth.enabled=false \
  --set image.repository=redis --set image.tag=8   # Redis 8 bundles the JSON/Search modules the checkpointer needs
helm install concierge-qdrant qdrant/qdrant

# 3. secrets (never commit these)
kubectl create secret generic concierge-postgresql --from-literal=password=change-me --from-literal=postgres-password=change-me
kubectl create secret generic concierge-secrets \
  --from-literal=ANTHROPIC_API_KEY=... --from-literal=APPROVAL_SECRET=$(openssl rand -hex 32) \
  --from-literal=OPERATOR_API_KEY=$(openssl rand -hex 16)

# 4. Concierge (image built by CI and pushed to GHCR)
helm install concierge deploy/helm/concierge \
  --set image.repository=ghcr.io/<you>/concierge --set image.tag=<sha> \
  --set ingress.host=concierge.localhost
```

Open http://concierge.localhost:8080 (chat), `/operator` (approvals and handoffs) and `/ops` (latency, cost, traces).

Scaling notes:
- The gateway is stateless (conversation state lives in the Redis checkpointer, sessions/outbox in Redis), so the HPA
  can add replicas freely. Per-session ordering is enforced in-process; with several replicas, route a session to one
  pod (sticky sessions on the session id) or add a Redis lock around `handle()`.
- The backends are stateless over Postgres. The MCP server runs in stateless Streamable HTTP mode, so it scales horizontally.
- The ingest Job is a Helm hook; it only re-embeds when the policy corpus fingerprint changes.
