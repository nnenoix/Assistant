"""Phase 0 scaffold: empty schema.

Revision ID: 001_phase0_scaffold
Revises:
Create Date: 2026-05-23

This migration is a no-op. It establishes the alembic version table so
subsequent revisions can build the real schema (users, tenants, audit_log,
approvals, mdm_*, kpi_history, traces) incrementally without rewriting
existing data on every change.
"""
from alembic import op


revision = "001_phase0_scaffold"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nothing to do — alembic creates the alembic_version table on its own.
    # Subsequent revisions add real tables.
    op.execute("SELECT 1")


def downgrade() -> None:
    pass
