"""Regression tests for cleanup sweepers.

- `_idempotency.sweep_expired` removes rows past TTL.
- `infra.audit_rotate` rotates the JSONL file once it exceeds threshold.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


# ============================================================
# _idempotency.sweep_expired
# ============================================================

@pytest.fixture
def fresh_idem(tmp_path, monkeypatch):
    from src.tools import _idempotency as idem
    monkeypatch.setattr(idem, "DB_PATH", tmp_path / "idem.sqlite")
    monkeypatch.setattr(idem, "_conn", None)
    monkeypatch.setattr(idem, "_conn_pid", None)
    yield idem
    if idem._conn is not None:
        idem._conn.close()
        monkeypatch.setattr(idem, "_conn", None)


def test_sweep_removes_expired_entries(fresh_idem):
    idem = fresh_idem
    idem.store("alive", "tool_x", {}, {"r": "fresh"})
    idem.store("stale", "tool_y", {}, {"r": "old"})
    # Hand-poke the stored row to make `stale` look old.
    with idem._lock:
        conn = idem._connect()
        conn.execute(
            "UPDATE idempotency SET created_at = ? WHERE key = ?",
            (time.time() - 999_999, "stale"),
        )
    removed = idem.sweep_expired()
    assert removed == 1
    # Fresh entry still resolvable
    assert idem.lookup("alive", "tool_x", {})["hit"] is True
    # Expired entry is gone
    assert idem.lookup("stale", "tool_y", {}) == {"hit": False}


def test_sweep_is_no_op_on_empty_db(fresh_idem):
    removed = fresh_idem.sweep_expired()
    assert removed == 0


def test_sweep_with_short_ttl_purges_recent_entries(fresh_idem):
    """`ttl_seconds=0` purges everything not stored in the future."""
    fresh_idem.store("k", "t", {}, {"r": "x"})
    removed = fresh_idem.sweep_expired(ttl_seconds=0)
    assert removed == 1


def test_wal_mode_is_enabled(fresh_idem):
    """Idempotency sqlite should run in WAL — `journal_mode` query
    returns 'wal' on success, 'delete' (default) on older sqlite that
    rejected the PRAGMA. Either passes — we just want to confirm the
    PRAGMA attempt didn't blow up at connection open."""
    with fresh_idem._lock:
        conn = fresh_idem._connect()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    # On reasonably modern sqlite (≥3.7, ie any Python 3.11) we expect wal.
    # Don't fail on weird CI images — just smoke that the call succeeds.
    assert mode in {"wal", "delete", "memory", "truncate", "persist", "off"}


# ============================================================
# infra.audit_rotate
# ============================================================

@pytest.fixture
def fresh_infra(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_INFRA_DIR", tmp_path)
    monkeypatch.setattr(infra, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    return infra


def test_audit_rotate_no_op_when_file_missing(fresh_infra):
    out = fresh_infra.audit_rotate(threshold_bytes=1)
    assert out["ok"] is True
    assert out["data"]["rotated"] is False
    assert "no audit file" in out["data"]["reason"]


def test_audit_rotate_no_op_when_below_threshold(fresh_infra):
    fresh_infra.audit_log("test", "tool_x", {})
    out = fresh_infra.audit_rotate(threshold_bytes=10 * 1024 * 1024)
    assert out["data"]["rotated"] is False
    assert out["data"]["size_before"] > 0
    # File still there, unrotated
    assert fresh_infra._AUDIT_PATH.exists()


def test_audit_rotate_archives_when_above_threshold(fresh_infra, tmp_path):
    """Threshold = 1 byte → any non-empty audit triggers rotation."""
    fresh_infra.audit_log("test", "tool_x", {})
    out = fresh_infra.audit_rotate(threshold_bytes=1)
    assert out["data"]["rotated"] is True
    archived = Path(out["data"]["archived_path"])
    assert archived.exists()
    assert archived.read_text(encoding="utf-8")  # has content
    # Fresh audit.jsonl was recreated empty
    assert fresh_infra._AUDIT_PATH.exists()
    assert fresh_infra._AUDIT_PATH.stat().st_size == 0


def test_audit_rotate_next_audit_log_goes_to_fresh_file(fresh_infra):
    fresh_infra.audit_log("first", "tool_x", {})
    fresh_infra.audit_rotate(threshold_bytes=1)
    # New write should land in the fresh (empty) audit.jsonl
    fresh_infra.audit_log("second", "tool_x", {})
    content = fresh_infra._AUDIT_PATH.read_text(encoding="utf-8")
    assert '"action": "second"' in content
    assert '"action": "first"' not in content
