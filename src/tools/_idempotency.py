"""Idempotency-key store for non-idempotent tools (Stripe-style).

Caller passes `idempotency_key` (UUID-like string) on a destructive or
otherwise-non-idempotent tool call. If the same key has been seen within
the TTL (24h default) AND the args hash matches, the cached response is
replayed without re-executing the tool. This makes accidental retries
safe — e.g. a flaky network that causes the agent to re-invoke
`gmail_send_draft` will deliver exactly one email instead of two.

Storage: a single sqlite file at ``.data/idempotency.sqlite``. Thread-safe
via process-level lock. Off when no key is provided — preserves existing
behaviour for callers that don't opt in.

Multi-tenant: rows are keyed by `(tenant_id, key, tool)` so a malicious
caller can't poison or read another tenant's cache by guessing an
idempotency key. The tenant is resolved from `src.tenancy.current_tenant_id()`
at lookup/store time — no signature change required at the call site.

Reference: Stripe's «Designing robust and predictable APIs with
idempotency» (2017). Same args + same key → cached. Same key + different
args → error (idempotency-key reused with different payload).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.tenancy import current_tenant_id


DEFAULT_TTL_S = 24 * 60 * 60  # 24 hours
DB_PATH = DATA_DIR / "idempotency.sqlite"


_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# Track the PID that opened the sqlite handle. arq workers + uvicorn
# multi-worker setups fork the parent process — the inherited FD points
# at the same WAL, and concurrent writes corrupt it. Re-open on PID
# mismatch so each child process gets its own handle.
_conn_pid: int | None = None


def _has_tenant_column(conn: sqlite3.Connection) -> bool:
    cols = conn.execute("PRAGMA table_info(idempotency)").fetchall()
    return any(c[1] == "tenant_id" for c in cols)


def _connect() -> sqlite3.Connection:
    global _conn, _conn_pid
    pid = os.getpid()
    if _conn is not None and _conn_pid == pid:
        return _conn
    if _conn is not None and _conn_pid != pid:
        # Inherited handle from a fork — DO NOT call .close() on it
        # (that would mess with the parent's WAL too). Just orphan it
        # and reopen for our PID. The parent / sibling processes keep
        # their own copies.
        _conn = None
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
    # WAL mode: concurrent readers don't block writers (vs. the default
    # "rollback journal" which serializes everything). Tradeoff: leaves
    # `-wal` + `-shm` files next to the .sqlite. NB: PRAGMA must be set
    # AFTER connection open; failing silently is fine (busy older sqlite).
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe + faster
    except Exception:
        pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idempotency ("
        " tenant_id TEXT NOT NULL DEFAULT 'default',"
        " key TEXT NOT NULL,"
        " tool TEXT NOT NULL,"
        " args_hash TEXT NOT NULL,"
        " response_json TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " PRIMARY KEY (tenant_id, key, tool)"
        ")"
    )
    # Live-migration for stores created before the tenant column existed.
    # Rather than ALTER TABLE (which would force a PK rebuild anyway in
    # sqlite), drop the cache: it's a 24h TTL store, callers retry safely.
    if not _has_tenant_column(conn):
        conn.execute("DROP TABLE idempotency")
        conn.execute(
            "CREATE TABLE idempotency ("
            " tenant_id TEXT NOT NULL DEFAULT 'default',"
            " key TEXT NOT NULL,"
            " tool TEXT NOT NULL,"
            " args_hash TEXT NOT NULL,"
            " response_json TEXT NOT NULL,"
            " created_at REAL NOT NULL,"
            " PRIMARY KEY (tenant_id, key, tool)"
            ")"
        )
    _conn = conn
    _conn_pid = pid
    return conn


def _hash_args(args: dict[str, Any]) -> str:
    """Deterministic args hash. Sort keys; ignore the idempotency_key itself."""
    safe = {k: v for k, v in args.items() if k != "idempotency_key"}
    blob = json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lookup(key: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return {hit, response?, mismatch?, age_seconds?}.

    - hit=True + response: serve from cache; do NOT re-execute the tool.
    - hit=True + mismatch=True: same key was used with different args — abort.
    - hit=False: caller proceeds with normal execution then calls `store()`.

    Looks up under the current request's tenant — see module docstring.
    """
    if not key:
        return {"hit": False}
    tenant = current_tenant_id()
    args_hash = _hash_args(args)
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT args_hash, response_json, created_at FROM idempotency "
            "WHERE tenant_id = ? AND key = ? AND tool = ?",
            (tenant, key, tool),
        ).fetchone()
    if row is None:
        return {"hit": False}
    stored_hash, response_json, created_at = row
    age = time.time() - created_at
    if age > DEFAULT_TTL_S:
        # Expired — caller will overwrite via store().
        return {"hit": False, "expired": True}
    if stored_hash != args_hash:
        return {
            "hit": True,
            "mismatch": True,
            "age_seconds": age,
        }
    return {
        "hit": True,
        "response": json.loads(response_json),
        "age_seconds": age,
    }


def store(key: str, tool: str, args: dict[str, Any], response: dict[str, Any]) -> None:
    """Persist `response` keyed by (tenant, key, tool). No-op when `key` is empty."""
    if not key:
        return
    tenant = current_tenant_id()
    args_hash = _hash_args(args)
    response_json = json.dumps(response, ensure_ascii=False, default=str)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO idempotency "
            "(tenant_id, key, tool, args_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant, key, tool, args_hash, response_json, now),
        )


def clear() -> int:
    """Drop everything. Returns count deleted. Used by tests + manual reset."""
    with _lock:
        conn = _connect()
        before = conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
        conn.execute("DELETE FROM idempotency")
        return before


def sweep_expired(ttl_seconds: float | None = None) -> int:
    """Delete expired idempotency entries. Returns count removed.

    Callers: arq cron job (hourly), or manual `python -m
    scripts.idempotency_sweep`. Without this the sqlite file grows
    unbounded — entries past TTL are functionally inert (lookup returns
    `hit: False, expired: True`) but they still consume disk.

    Pass `ttl_seconds=None` to use the module's DEFAULT_TTL_S; pass a
    custom value (e.g. 3600 for hourly cleanup with a 1h TTL override)
    only for tests.
    """
    ttl = DEFAULT_TTL_S if ttl_seconds is None else ttl_seconds
    cutoff = time.time() - ttl
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM idempotency WHERE created_at < ?",
            (cutoff,),
        )
        # Reclaim disk space — sqlite VACUUM is expensive but the sweep
        # already runs off the hot path. Skip if delete touched nothing.
        if cur.rowcount > 0:
            try:
                conn.execute("VACUUM")
            except sqlite3.OperationalError:
                # VACUUM inside a transaction fails; we're autocommit so
                # this is the "another writer" case — defer to next sweep.
                pass
        return cur.rowcount or 0


def _reset_for_tests(tmp_path: Path) -> None:
    """Point the singleton at a tmp file. Tests only."""
    global _conn, _conn_pid, DB_PATH
    if _conn is not None:
        _conn.close()
        _conn = None
    _conn_pid = None
    DB_PATH = tmp_path / "idempotency.sqlite"
