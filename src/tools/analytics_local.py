"""Local analytics via DuckDB (in-process columnar engine).

DuckDB is the fast cousin of SQLite for OLAP queries — joins, aggregates,
window functions over millions of rows on a single laptop. Use when the
agent needs to crunch a CSV / JSONL / parquet file or do ad-hoc SQL
against a Sheets export.

Storage: `.data/duckdb.duckdb`. Tables persist across calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import DATA_DIR

DB_PATH = DATA_DIR / "duckdb.duckdb"


def _connect():
    """Lazy import + per-call connection (DuckDB is connection-safe but
    fast to open)."""
    try:
        import duckdb
    except ImportError:
        return None, {"ok": False, "error": "duckdb not installed",
                      "fix_hint": "pip install duckdb"}
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH)), None


def duckdb_query(sql: str, max_rows: int = 1000) -> dict:
    """Run a read-only SQL query against the local DuckDB. Returns rows up
    to `max_rows`. Supports reading external files inline:
        SELECT * FROM read_csv_auto('path/to/file.csv') WHERE ..."""
    conn, err = _connect()
    if err:
        return err
    try:
        res = conn.execute(sql).fetch_arrow_table().to_pylist()
        truncated = len(res) > max_rows
        return {
            "ok": True,
            "data": {"rows": res[:max_rows], "row_count": len(res)},
            "_meta": {"truncated": truncated, "max_rows": max_rows},
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "_meta": {}}
    finally:
        conn.close()


def duckdb_import_csv(table: str, path: str, replace: bool = False) -> dict:
    """Create or append to a DuckDB table from a local CSV. Pass
    `replace=True` to overwrite existing rows."""
    conn, err = _connect()
    if err:
        return err
    try:
        ctas = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
        conn.execute(f"{ctas} {table} AS SELECT * FROM read_csv_auto('{path}')")
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {"ok": True, "data": {"table": table, "row_count": row_count}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        conn.close()


def duckdb_list_tables() -> dict:
    """List all tables + row counts in the local DuckDB."""
    conn, err = _connect()
    if err:
        return err
    try:
        names = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        out = []
        for n in names:
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
                cols = conn.execute(f"DESCRIBE {n}").fetchall()
                out.append({"name": n, "row_count": cnt,
                            "columns": [{"name": c[0], "type": c[1]} for c in cols]})
            except Exception:
                out.append({"name": n, "error": "describe failed"})
        return {"ok": True, "data": {"tables": out}}
    finally:
        conn.close()


def duckdb_drop_table(table: str) -> dict:
    """Drop a table."""
    conn, err = _connect()
    if err:
        return err
    try:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        return {"ok": True, "data": {"dropped": table}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        conn.close()


def duckdb_export_parquet(table: str, path: str) -> dict:
    """Export a table to a parquet file."""
    conn, err = _connect()
    if err:
        return err
    try:
        conn.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET)")
        size = Path(path).stat().st_size if Path(path).exists() else None
        return {"ok": True, "data": {"path": path, "bytes": size}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        conn.close()
