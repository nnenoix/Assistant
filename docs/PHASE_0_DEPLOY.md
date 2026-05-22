# Phase 0 deployment runbook

End-to-end runbook for standing up the command-service stack on a fresh
Linux box. Walks through: prerequisites → bring-up → Authentik bootstrap
→ migrations → smoke verify → connect LibreChat → observability → backup.

## Prerequisites

- Docker Engine 24+ with `docker compose` plugin
- 4 GB RAM minimum (Authentik alone uses ~700 MB)
- 10 GB disk for Postgres + Redis + Authentik data
- Ports 8765 (agent), 9000/9443 (Authentik), 5432 (postgres),
  6379 (redis), 4317/4318 (OTel) open locally OR proxied behind reverse
  proxy (recommended for prod — nginx / Traefik)

## 1. Initial bring-up

```bash
git clone <repo>
cd workspace-agent
cp .env.example .env
# Edit .env — at minimum set:
#   POSTGRES_PASSWORD=$(openssl rand -base64 32)
#   AUTHENTIK_SECRET_KEY=$(openssl rand -base64 64)
docker compose up -d
docker compose logs -f agent | head -50  # verify clean start
```

Verify with:
```bash
curl http://localhost:8765/health
# {"status":"ok"}
```

## 2. Bootstrap Authentik

```bash
docker compose exec authentik /bin/bash -lc \
    'ak create_admin_group --user akadmin --create'
export AUTHENTIK_BOOTSTRAP_PASSWORD=...  # whatever you set during the
                                          # initial-setup flow
./scripts/bootstrap_authentik.sh
# Script prints OIDC_CLIENT_ID/SECRET — paste into .env, then:
docker compose restart agent
```

## 3. Run database migrations

```bash
docker compose exec agent uv run alembic upgrade head
# Should print: 002_phase0_core_schema applied successfully
```

## 4. Smoke verify

```bash
# Discovery — should return ~403 tools
curl -H "Authorization: Bearer $OIDC_TEST_TOKEN" \
    http://localhost:8765/mcp | jq .result.count

# Invoke a read-only tool
curl -X POST http://localhost:8765/mcp \
    -H "Authorization: Bearer $OIDC_TEST_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
         "params":{"name":"nlp_extract_inns",
                   "arguments":{"text":"ИНН 7707083893"}}}'
```

## 5. Wire LibreChat (optional)

```bash
git clone https://github.com/danny-avila/LibreChat.git
cd LibreChat
cp ../workspace-agent/config/librechat.example.yaml ./librechat.yaml
# Edit librechat.yaml — replace ${AGENT_BEARER} with a real service-account JWT
docker compose up -d
# Visit http://localhost:3080 — log in, the workspace-agent tools should
# show up in the MCP servers dropdown.
```

## 6. Observability

### OpenTelemetry → Langfuse self-hosted

```bash
# Start Langfuse alongside:
docker run -d --name langfuse -p 3000:3000 \
    -e DATABASE_URL=postgresql://agent:$POSTGRES_PASSWORD@postgres:5432/langfuse \
    langfuse/langfuse:latest
# Get an API key from the Langfuse UI, then:
echo "LANGFUSE_HOST=http://langfuse:3000" >> .env
echo "LANGFUSE_AUTH_B64=$(echo -n 'pk-...:sk-...' | base64)" >> .env
# Uncomment the otlphttp/langfuse exporter in otel-collector-config.yaml,
# then restart:
docker compose restart otel_collector
```

### Arize Phoenix self-hosted

```bash
docker run -d --name phoenix -p 6006:6006 arizephoenix/phoenix:latest
# Uncomment otlp/phoenix exporter in otel-collector-config.yaml + restart.
```

### Prometheus

```bash
docker run -d --name prometheus -p 9090:9090 \
    -v "$(pwd)/config/prometheus.yml":/etc/prometheus/prometheus.yml:ro \
    prom/prometheus:latest
```

## 7. Backup / restore

### Postgres dump (daily cron, locally)

```bash
# In crontab (host):
0 3 * * * docker compose exec -T postgres pg_dump -U agent workspace_agent \
    | gzip > /backups/wsa-$(date +\%F).sql.gz
```

### Restore

```bash
gunzip < /backups/wsa-2026-05-23.sql.gz | \
    docker compose exec -T postgres psql -U agent workspace_agent
```

### Migration from file-backed `.data/infra/*.jsonl` to Postgres

Currently the tool implementations write to `.data/infra/*.jsonl`. To
migrate to Postgres for multi-instance / multi-tenant deployment:

1. Run `alembic upgrade head` (creates the schema)
2. Set `USE_POSTGRES_STORAGE=1` in `.env`
3. Run the migration helper:
   ```bash
   docker compose exec agent uv run python -m scripts.migrate_jsonl_to_pg
   ```
   (script: NOT yet written — followup. It walks each `.data/infra/*.jsonl`
   and INSERTS into the matching table, tagging every row with the
   default tenant_id.)

## 8. Operational quick reference

| Task | Command |
|---|---|
| View logs | `docker compose logs -f agent` |
| Restart agent only | `docker compose restart agent` |
| Open psql | `docker compose exec postgres psql -U agent workspace_agent` |
| Check queue depth | `docker compose exec redis redis-cli LLEN arq:queue` |
| Trigger arq job | `docker compose exec agent uv run python -c "from arq import create_pool; ..."` |
| Reset all data (DANGEROUS) | `docker compose down -v` |

## 9. Known followups (intentionally NOT in this scaffolding)

- `scripts/migrate_jsonl_to_pg` migration script
- `metrics_path: /metrics` on the FastAPI app (needs `prometheus-fastapi-instrumentator`)
- Real OTel instrumentation in tool wrappers (currently only `service.trace_span_log` jsonl-stub)
- ЮKassa cert rotation automation (cert is published, need polling)
- LibreChat OIDC SSO integration (currently uses service-account Bearer token)
- Helm chart / Kubernetes manifests for non-Docker deploys
- Real CRDs for multi-tenant scaling
