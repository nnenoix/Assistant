"""One-shot migration tool: `.data/infra/*.jsonl` → Postgres tables.

Run when transitioning a single-instance deployment (file-backed
storage in `.data/infra/`) to a multi-tenant / multi-instance setup
backed by Postgres. The Alembic schema in `migrations/versions/
002_phase0_core_schema.py` must already be applied (`alembic upgrade
head`) before this runs.

Source → target mapping:
    .data/infra/approvals.jsonl      → approvals        (PK approval_id)
    .data/infra/audit.jsonl          → audit_log        (autoincrement)
    .data/infra/kpi_history.jsonl    → kpi_history      (autoincrement)
    .data/infra/mdm/<table>.json     → mdm_records      (UNIQUE tenant_id+table+id)

Idempotency:
    - approvals / mdm_records: natural keys → UPSERT semantics. Re-running
      is safe; later JSONL state wins.
    - audit_log / kpi_history: no natural key. Re-running would duplicate;
      the script refuses unless `--allow-duplicates` is passed.

Modes:
    --dry-run (default)       : parse + classify; do NOT write to DB.
    --apply                   : actually write.
    --dsn <uri>               : DB URI; default `$PG_DSN` env or
                                `postgresql://agent@localhost/workspace_agent`.
    --tenant-id <id>          : tag every imported row with this tenant.
                                Default 'default' (matches the seed row
                                Alembic 002 creates).
    --data-dir <path>         : override the source dir (default `.data/infra`).
    --allow-duplicates        : skip the "rows-already-present" guard on
                                audit_log / kpi_history.

Exit codes:
    0  — success (or clean dry-run preview)
    1  — partial or full failure (some rows refused / DB error)
    2  — refused (allow-duplicates guard tripped, no --apply on apply path)

Driver: `psycopg2` for `postgresql://` DSNs, stdlib `sqlite3` for
`sqlite://...` (used by tests). Other dialects raise at import time.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("migrate_jsonl_to_pg")

DEFAULT_DATA_DIR = Path(".data/infra")
DEFAULT_TENANT_ID = "default"


# ============================================================
# Source readers — pure functions, no DB
# ============================================================

def _iter_jsonl(path: Path) -> Iterable[dict]:
    """Yield parsed records from a .jsonl file. Skips blank and
    malformed lines with a warning so a half-corrupt file still
    migrates the good rows."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("%s line %d: malformed JSON (%s) — skipped",
                               path.name, lineno, e)


def _iter_json_array(path: Path) -> Iterable[dict]:
    """Yield records from a JSON-array file (used by `.data/infra/mdm/*.json`)."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("%s: malformed JSON (%s) — skipped", path.name, e)
        return
    if not isinstance(data, list):
        logger.warning("%s: expected list, got %s — skipped",
                       path.name, type(data).__name__)
        return
    yield from data


# ============================================================
# Per-source migrators
# ============================================================

@dataclass
class MigrationReport:
    source: str
    target_table: str
    total_records: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _q(dialect: str) -> str:
    """Placeholder character — sqlite3 uses '?', psycopg2 uses '%s'."""
    return "?" if dialect == "sqlite" else "%s"


def migrate_approvals(conn, dialect: str, data_dir: Path, tenant_id: str,
                      dry_run: bool) -> MigrationReport:
    """approvals.jsonl is append-only — multiple rows per approval_id
    (one for the request, one for the decision). We collapse them: the
    LATEST row for an approval_id wins, then UPSERT into Postgres."""
    src = data_dir / "approvals.jsonl"
    report = MigrationReport(source=str(src), target_table="approvals")
    latest: dict[str, dict] = {}
    for rec in _iter_jsonl(src):
        aid = rec.get("approval_id")
        if not aid:
            report.errors.append(f"missing approval_id in row: {str(rec)[:120]}")
            continue
        latest[aid] = rec
    report.total_records = len(latest)
    if not latest or dry_run:
        return report

    p = _q(dialect)
    existing = {
        r[0] for r in
        conn.execute(f"SELECT approval_id FROM approvals WHERE tenant_id = {p}",
                     (tenant_id,)).fetchall()
    }
    for aid, rec in latest.items():
        params = (
            aid, tenant_id,
            rec.get("status", "pending"),
            rec.get("action") or "",
            json.dumps(rec.get("args") or {}, ensure_ascii=False),
            rec.get("requested_by"),
            rec.get("reason"),
            rec.get("requested_at"),
            rec.get("decided_by"),
            rec.get("decided_at"),
            rec.get("note"),
        )
        if aid in existing:
            conn.execute(
                f"UPDATE approvals SET status={p}, action={p}, args_json={p}, "
                f"requested_by_sub={p}, reason={p}, requested_at={p}, "
                f"decided_by_sub={p}, decided_at={p}, note={p} "
                f"WHERE approval_id={p} AND tenant_id={p}",
                (rec.get("status", "pending"), rec.get("action") or "",
                 json.dumps(rec.get("args") or {}, ensure_ascii=False),
                 rec.get("requested_by"), rec.get("reason"),
                 rec.get("requested_at"), rec.get("decided_by"),
                 rec.get("decided_at"), rec.get("note"),
                 aid, tenant_id),
            )
            report.updated += 1
        else:
            conn.execute(
                f"INSERT INTO approvals "
                f"(approval_id, tenant_id, status, action, args_json, "
                f" requested_by_sub, reason, requested_at, decided_by_sub, "
                f" decided_at, note) VALUES "
                f"({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
                params,
            )
            report.inserted += 1
    return report


def migrate_audit_log(conn, dialect: str, data_dir: Path, tenant_id: str,
                      dry_run: bool, allow_duplicates: bool) -> MigrationReport:
    """audit.jsonl has no natural key — bare append-only. Guard against
    accidental double-imports unless `--allow-duplicates` says it's fine."""
    src = data_dir / "audit.jsonl"
    report = MigrationReport(source=str(src), target_table="audit_log")
    rows = list(_iter_jsonl(src))
    report.total_records = len(rows)
    if not rows or dry_run:
        return report

    p = _q(dialect)
    if not allow_duplicates:
        existing = conn.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE tenant_id = {p}",
            (tenant_id,),
        ).fetchone()[0]
        if existing > 0:
            report.errors.append(
                f"audit_log already has {existing} rows for tenant_id="
                f"{tenant_id!r}. Refusing to add more — pass "
                f"--allow-duplicates if this is intended."
            )
            report.skipped = len(rows)
            return report

    for rec in rows:
        conn.execute(
            f"INSERT INTO audit_log (ts, tenant_id, actor_sub, tool, action, "
            f" args_json, result_summary, correlation_id) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
            (rec.get("ts"), tenant_id, rec.get("actor"),
             rec.get("tool") or "", rec.get("action") or "",
             json.dumps(rec.get("args_summary") or {}, ensure_ascii=False),
             rec.get("result_summary"), rec.get("correlation_id")),
        )
        report.inserted += 1
    return report


def migrate_kpi_history(conn, dialect: str, data_dir: Path, tenant_id: str,
                        dry_run: bool, allow_duplicates: bool) -> MigrationReport:
    """kpi_history.jsonl — same shape as audit_log (no natural key)."""
    src = data_dir / "kpi_history.jsonl"
    report = MigrationReport(source=str(src), target_table="kpi_history")
    rows = list(_iter_jsonl(src))
    report.total_records = len(rows)
    if not rows or dry_run:
        return report

    p = _q(dialect)
    if not allow_duplicates:
        existing = conn.execute(
            f"SELECT COUNT(*) FROM kpi_history WHERE tenant_id = {p}",
            (tenant_id,),
        ).fetchone()[0]
        if existing > 0:
            report.errors.append(
                f"kpi_history already has {existing} rows for tenant_id="
                f"{tenant_id!r}. Pass --allow-duplicates to import anyway."
            )
            report.skipped = len(rows)
            return report

    for rec in rows:
        if rec.get("value") is None or not rec.get("name"):
            report.errors.append(f"kpi row missing name/value: {str(rec)[:120]}")
            continue
        conn.execute(
            f"INSERT INTO kpi_history (tenant_id, name, value, ts, tags_json) "
            f"VALUES ({p}, {p}, {p}, {p}, {p})",
            (tenant_id, rec["name"], float(rec["value"]),
             rec.get("ts"),
             json.dumps(rec.get("tags") or {}, ensure_ascii=False)),
        )
        report.inserted += 1
    return report


def migrate_mdm(conn, dialect: str, data_dir: Path, tenant_id: str,
                dry_run: bool) -> MigrationReport:
    """Walk every .data/infra/mdm/<table>.json file. Each is a JSON
    array of {id, external_ids, fields, created_at, updated_at}. Maps to
    `mdm_records` with `table_name` ← filename stem."""
    mdm_dir = data_dir / "mdm"
    report = MigrationReport(source=str(mdm_dir), target_table="mdm_records")
    if not mdm_dir.exists():
        return report

    p = _q(dialect)
    # Lazy import — keeps the script's module-level imports light when
    # only the parser bits are needed (tests, dry-run).
    from src.tools._safe_id import is_safe_id
    for path in sorted(mdm_dir.glob("*.json")):
        table_name = path.stem
        # Validate against the same contract `infra._safe_table` enforces
        # so we don't import names that the live tools would later refuse.
        if not is_safe_id(table_name):
            report.errors.append(f"skipping unsafe table name: {table_name!r}")
            continue
        records = list(_iter_json_array(path))
        report.total_records += len(records)
        if dry_run:
            continue
        existing = {
            r[0] for r in
            conn.execute(
                f"SELECT record_id FROM mdm_records WHERE tenant_id = {p} "
                f"AND table_name = {p}",
                (tenant_id, table_name),
            ).fetchall()
        }
        for rec in records:
            rid = rec.get("id")
            if not rid:
                report.errors.append(
                    f"{path.name}: row missing id — {str(rec)[:120]}"
                )
                continue
            ext = json.dumps(rec.get("external_ids") or {}, ensure_ascii=False)
            fields = json.dumps(rec.get("fields") or {}, ensure_ascii=False)
            if rid in existing:
                conn.execute(
                    f"UPDATE mdm_records SET external_ids={p}, fields={p}, "
                    f"updated_at={p} WHERE tenant_id={p} AND table_name={p} "
                    f"AND record_id={p}",
                    (ext, fields, rec.get("updated_at"),
                     tenant_id, table_name, rid),
                )
                report.updated += 1
            else:
                conn.execute(
                    f"INSERT INTO mdm_records "
                    f"(tenant_id, table_name, record_id, external_ids, "
                    f" fields, created_at, updated_at) VALUES "
                    f"({p}, {p}, {p}, {p}, {p}, {p}, {p})",
                    (tenant_id, table_name, rid, ext, fields,
                     rec.get("created_at"), rec.get("updated_at")),
                )
                report.inserted += 1
    return report


# ============================================================
# DB connection + entry point
# ============================================================

def _connect(dsn: str):
    """Return a DB-API connection + dialect string. psycopg2 for
    postgres, sqlite3 for sqlite, anything else → error."""
    if dsn.startswith("sqlite://"):
        import sqlite3
        path = dsn.replace("sqlite:///", "", 1) or ":memory:"
        return sqlite3.connect(path, isolation_level=None), "sqlite"
    if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
        try:
            import psycopg2  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "psycopg2 is required for postgres DSNs — "
                "`pip install psycopg2-binary`"
            ) from e
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn, "postgres"
    raise ValueError(f"unsupported DSN scheme in {dsn!r}")


def run_migration(dsn: str, data_dir: Path, tenant_id: str,
                  dry_run: bool, allow_duplicates: bool) -> list[MigrationReport]:
    """Drive the four per-source migrators. Returns a list of reports
    in the same order so the caller can render a summary."""
    if dry_run:
        conn = None
        dialect = "sqlite"  # placeholder — never used in dry-run
        reports = [
            migrate_approvals(conn, dialect, data_dir, tenant_id, dry_run=True),
            migrate_audit_log(conn, dialect, data_dir, tenant_id,
                              dry_run=True, allow_duplicates=allow_duplicates),
            migrate_kpi_history(conn, dialect, data_dir, tenant_id,
                                dry_run=True, allow_duplicates=allow_duplicates),
            migrate_mdm(conn, dialect, data_dir, tenant_id, dry_run=True),
        ]
        return reports

    conn, dialect = _connect(dsn)
    try:
        cursor_conn = _Cursor(conn)
        reports = [
            migrate_approvals(cursor_conn, dialect, data_dir, tenant_id, False),
            migrate_audit_log(cursor_conn, dialect, data_dir, tenant_id,
                              False, allow_duplicates),
            migrate_kpi_history(cursor_conn, dialect, data_dir, tenant_id,
                                False, allow_duplicates),
            migrate_mdm(cursor_conn, dialect, data_dir, tenant_id, False),
        ]
    finally:
        conn.close()
    return reports


class _Cursor:
    """Tiny shim: both sqlite3.Connection.execute and psycopg2 cursors
    expose `.execute(sql, params).fetchone()/.fetchall()`, but psycopg2
    requires a cursor object first. Normalize so the migrator code is
    identical for both backends."""
    def __init__(self, conn):
        self._conn = conn
        self._is_psycopg = hasattr(conn, "cursor") and not hasattr(conn, "executemany")

    def execute(self, sql: str, params: tuple = ()) -> "_Cursor":
        if self._is_psycopg:
            self._cur = self._conn.cursor()
            self._cur.execute(sql, params)
            return self
        self._cur = self._conn.execute(sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


def _print_report(reports: list[MigrationReport], dry_run: bool) -> bool:
    """Render a human-readable summary. Returns True if all clean."""
    print()
    print("=" * 72)
    print(f"Migration {'DRY-RUN' if dry_run else 'APPLY'} report")
    print("=" * 72)
    any_errors = False
    for r in reports:
        line = (
            f"  {r.target_table:<14} from {r.source}: "
            f"{r.total_records:>4} rows | "
            f"insert={r.inserted} update={r.updated} skipped={r.skipped}"
        )
        if r.errors:
            line += f" ERRORS={len(r.errors)}"
            any_errors = True
        print(line)
        for e in r.errors[:5]:
            print(f"      - {e}")
        if len(r.errors) > 5:
            print(f"      … and {len(r.errors) - 5} more")
    print("=" * 72)
    return not any_errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_jsonl_to_pg",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually write to DB (default: dry-run)")
    parser.add_argument("--dsn", default=os.environ.get(
        "PG_DSN", "postgresql://agent@localhost/workspace_agent"))
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--allow-duplicates", action="store_true",
                        help="bypass the rows-already-present guard on "
                             "audit_log / kpi_history (DANGEROUS — duplicates)")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.data_dir.exists():
        print(f"error: --data-dir does not exist: {args.data_dir}", file=sys.stderr)
        return 1

    dry_run = not args.apply
    reports = run_migration(
        dsn=args.dsn,
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        dry_run=dry_run,
        allow_duplicates=args.allow_duplicates,
    )
    clean = _print_report(reports, dry_run)
    if dry_run:
        print("\n(dry-run — re-run with --apply to actually write)")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
