"""Alembic migration env — connects to Postgres via POSTGRES_DSN env.

Run migrations: `alembic upgrade head`
Create a new migration: `alembic revision -m "describe change"`

Phase 0 schema (incremental, applied as separate revisions):
  - users (id, sub, email, name, tenant_id, created_at)
  - tenants (id, name, owner_sub, created_at)
  - audit_log (id, ts, actor, tool, action, args_json, correlation_id)
  - approvals (id, status, action, args_json, requested_by, decided_by, decided_at)
  - mdm_products / mdm_suppliers / mdm_contractors (id, external_ids JSONB, fields JSONB)
  - kpi_history (id, name, value, ts, tags JSONB)
  - traces (id, span_id, parent_span_id, name, duration_ms, attributes JSONB)

For Phase 0, all of the above live as `.data/infra/*.jsonl` files (see
`src/tools/infra.py`). Migration #001 is the no-op SCAFFOLD that creates
the empty schema so subsequent revisions have a base.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_dsn() -> str:
    return os.environ.get(
        "POSTGRES_DSN",
        "postgresql+psycopg://agent:agent_dev_only@localhost:5432/workspace_agent",
    )


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` — produce raw SQL without DB connection."""
    context.configure(
        url=get_dsn(),
        target_metadata=None,  # Plain SQL migrations for Phase 0
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Standard alembic run against a live DB."""
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = get_dsn()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
