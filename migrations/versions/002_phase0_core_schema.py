"""Phase 0 core schema: tenants, users, audit_log, approvals, mdm_records,
kpi_history, traces, webhooks.

Revision ID: 002_phase0_core_schema
Revises: 001_phase0_scaffold
Create Date: 2026-05-23

Single migration that creates the Postgres-backed equivalents of the
file-based primitives in `src/tools/infra.py` + `src/tools/service.py`.
The application keeps writing to `.data/infra/*.jsonl` until a separate
config flag (`USE_POSTGRES_STORAGE=1`) routes them to these tables.

Multi-tenancy: every user/audit/approval/mdm/kpi row carries `tenant_id`
so a single deployment can serve multiple customers with hard isolation.
"""
from alembic import op
import sqlalchemy as sa


revision = "002_phase0_core_schema"
down_revision = "001_phase0_scaffold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("owner_sub", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("metadata_json", sa.JSON, nullable=True),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sub", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), index=True),
        sa.Column("name", sa.String(255)),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("groups_json", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("actor_sub", sa.String(255), index=True),
        sa.Column("tool", sa.String(255), nullable=False, index=True),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("args_json", sa.JSON),
        sa.Column("result_summary", sa.Text),
        sa.Column("correlation_id", sa.String(64), index=True),
    )

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),  # pending|approved|denied
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("args_json", sa.JSON),
        sa.Column("requested_by_sub", sa.String(255)),
        sa.Column("reason", sa.Text),
        sa.Column("requested_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_by_sub", sa.String(255)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
    )

    op.create_table(
        "mdm_records",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("table_name", sa.String(64), nullable=False, index=True),
        sa.Column("record_id", sa.String(128), nullable=False, index=True),
        sa.Column("external_ids", sa.JSON),  # {wb_nm, ozon_sku, ...}
        sa.Column("fields", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "table_name", "record_id"),
    )

    op.create_table(
        "kpi_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, index=True),
        sa.Column("tags_json", sa.JSON),
    )

    op.create_table(
        "traces",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), index=True),
        sa.Column("trace_id", sa.String(32), index=True),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("parent_span_id", sa.String(16)),
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("attributes_json", sa.JSON),
    )

    op.create_table(
        "webhooks",
        sa.Column("webhook_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  index=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, index=True),
        sa.Column("source", sa.String(64), nullable=False, index=True),
        sa.Column("payload_json", sa.JSON),
        sa.Column("headers_json", sa.JSON),
        sa.Column("signature_valid", sa.Boolean),
    )


def downgrade() -> None:
    for tab in ("webhooks", "traces", "kpi_history", "mdm_records",
                "approvals", "audit_log", "users", "tenants"):
        op.drop_table(tab)
