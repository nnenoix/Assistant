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

### 5a. Quick start — service-account bearer

For a single-user setup or a quick demo:

```bash
git clone https://github.com/danny-avila/LibreChat.git
cd LibreChat
cp ../workspace-agent/config/librechat.example.yaml ./librechat.yaml
# Edit librechat.yaml — replace ${AGENT_BEARER} with a real service-account JWT
docker compose up -d
# Visit http://localhost:3080 — log in, the workspace-agent tools should
# show up in the MCP servers dropdown.
```

### 5b. Production — shared OIDC SSO (recommended)

When the agent + LibreChat share the same Authentik (or Keycloak), end
users log in to LibreChat once and their verified JWT is forwarded to
the agent's `/mcp` endpoint. Per-user RBAC then applies on the agent
side — no service-account bearer needed.

```bash
# 1. In Authentik:
#    - Create an OAuth2/OpenID Provider (Client type: Confidential)
#    - Redirect URI:  https://librechat.example.com/oauth/openid/callback
#    - Scopes:        openid profile email groups
#    - Issuer / signing key: SAME as the agent's OIDC_ISSUER
#    - Create an Application bound to the Provider
#    - (Optional) Group-bind to restrict to `agent-users` group
#
# 2. Export the Provider's credentials:
export LIBRECHAT_OIDC_CLIENT_ID=...
export LIBRECHAT_OIDC_CLIENT_SECRET=...
export LIBRECHAT_OIDC_ISSUER=http://authentik:9000/application/o/workspace-agent/

# 3. Wire LibreChat with the OIDC example config:
cp ../workspace-agent/config/librechat-oidc.example.yaml ./librechat.yaml
docker compose up -d
```

User flow on the agent side:
- LibreChat → user signs in via Authentik
- LibreChat → forwards `Authorization: Bearer <user's OIDC token>` to `/mcp`
- Agent `_authenticate` verifies signature + audience + expiry against
  Authentik's JWKS (`OIDC_JWKS_URL`)
- Agent's RBAC + per-tenant idempotency cache use the claims directly
  — every audit-log row carries the real user's `sub`, not a shared
  service-account placeholder.

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
3. Preview what would migrate (safe — no writes):
   ```bash
   docker compose exec agent uv run python -m scripts.migrate_jsonl_to_pg
   ```
4. Apply it for real:
   ```bash
   docker compose exec agent uv run python -m scripts.migrate_jsonl_to_pg --apply
   ```

Source → target mapping:

| Source file | Target table | Idempotent re-run |
|---|---|---|
| `approvals.jsonl` | `approvals` | yes — UPSERT on approval_id |
| `audit.jsonl` | `audit_log` | no natural key — refuses re-run unless `--allow-duplicates` |
| `kpi_history.jsonl` | `kpi_history` | same as above |
| `mdm/<table>.json` | `mdm_records` | yes — UPSERT on (tenant_id, table_name, record_id) |

Flags:
- `--tenant-id <id>`: tag every imported row (default `default`).
- `--data-dir <path>`: override source dir (default `.data/infra`).
- `--allow-duplicates`: bypass audit/kpi rerun guard. DANGEROUS.

Exit codes: 0=clean, 1=errors logged in report, 2=refused.

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
- ~~`metrics_path: /metrics` on the FastAPI app~~ — **DONE.** Zero-dep
  Prometheus text emission via `src/metrics.py`; `_wrap_for_sdk`
  populates per-tool call counters + latency histogram.
- ~~Real OTel instrumentation in tool wrappers~~ — **DONE.** Each tool
  call is now a span with attributes `tool.name`, `tool.tenant_id`,
  `tool.dry_run`, `tool.idempotency_key_present`, `tool.status`,
  `tool.latency_ms`, `tool.error_kind`, `tool.quota_paced_ms`. On
  exception: `record_exception(e)` + `set_status(ERROR)`. No-op when
  `opentelemetry-api` isn't installed.
  (`service.trace_span_log` JSONL stub kept for offline replay.)
- ЮKassa cert rotation automation (cert is published, need polling)
- ~~LibreChat OIDC SSO integration~~ — **DONE.** See section 5b above
  + `config/librechat-oidc.example.yaml`. End users sign in via the
  same Authentik that secures the agent; their JWT is forwarded to
  `/mcp` so per-user RBAC + audit attribution apply.
- ~~Kubernetes manifests for non-Docker deploys~~ — **DONE.** Minimal
  single-replica set in `deploy/k8s/` (namespace, configmap, secret
  template, postgres StatefulSet, redis Deployment, agent Deployment +
  Service with Prometheus scrape annotations, ingress). See
  `deploy/k8s/README.md`. Helm chart still a followup.
- Helm chart with values.yaml + templates/ (HA / multi-replica)
- Real CRDs for multi-tenant scaling
