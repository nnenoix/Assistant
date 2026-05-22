# Phase 0 base image — slim FastAPI + uv + the agent code.
#
# This is the FOUNDATION for command-service deployment. The container
# exposes:
#   - /mcp           — MCP Streamable HTTP transport (when WSAgent.serve_http
#                      is the entrypoint)
#   - /api/*         — existing FastAPI routes from src/app.py
#   - /health        — liveness probe
#
# Runtime requirements:
#   - PostgreSQL (state, users, audit)         — pointed at via POSTGRES_DSN
#   - Redis (arq queue, distributed locks)     — pointed at via REDIS_URL
#   - Authentik (OIDC)                         — pointed at via OIDC_ISSUER
#   - OTel collector (optional, telemetry)     — pointed at via OTEL_EXPORTER_OTLP_ENDPOINT
#
# Build: docker build -t workspace-agent:phase0 .
# Run:   docker compose up -d (see docker-compose.yml)
FROM python:3.11-slim

# System deps: ca-certs for HTTPS, tini for proper PID 1 signal handling,
# libpq for psycopg, libxml2/libxslt for lxml (1С / EDO XML), poppler for PDF
# parsing fallback in mlhelpers.ocr_pdf.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini libpq-dev libxml2 libxslt1.1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# uv: fastest Python package installer, also handles project deps cleanly.
RUN pip install --no-cache-dir uv==0.5.0

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/
COPY static/ ./static/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8765/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uv", "run", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8765"]
