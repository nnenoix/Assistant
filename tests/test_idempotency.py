"""Stripe-style idempotency-key store + registry integration tests.

The store dedupes retries of non-idempotent destructive tools so a flaky
network can't deliver two emails / create two drafts. See
`src/tools/_idempotency.py`.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from src.tools import _idempotency as idem
from src.tools import registry


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Each test gets a fresh sqlite file so state doesn't leak between cases."""
    monkeypatch.setattr(idem, "DB_PATH", tmp_path / "idem.sqlite")
    monkeypatch.setattr(idem, "_conn", None)
    yield
    if idem._conn is not None:
        idem._conn.close()
        monkeypatch.setattr(idem, "_conn", None)


# ---------- store mechanics ----------

def test_store_then_lookup_hits():
    idem.store("k1", "tool_x", {"a": 1, "b": 2}, {"content": [{"type": "text", "text": "ok"}]})
    r = idem.lookup("k1", "tool_x", {"a": 1, "b": 2})
    assert r["hit"] is True
    assert r["response"]["content"][0]["text"] == "ok"


def test_lookup_miss_returns_no_hit():
    r = idem.lookup("never", "tool_x", {})
    assert r == {"hit": False}


def test_lookup_with_empty_key_is_no_op():
    """No key → no store check; preserves opt-in semantics."""
    idem.store("", "tool_x", {}, {"x": 1})
    r = idem.lookup("", "tool_x", {})
    assert r == {"hit": False}


def test_lookup_args_mismatch_flags_conflict():
    """Same key, different args → mismatch=True so caller can refuse."""
    idem.store("k2", "tool_y", {"a": 1}, {"ok": True})
    r = idem.lookup("k2", "tool_y", {"a": 2})
    assert r["hit"] is True
    assert r["mismatch"] is True


def test_args_hash_ignores_idempotency_key_field():
    """The key field itself must not change the args hash, else the second
    call (with the same key) would always look like a mismatch."""
    h1 = idem._hash_args({"a": 1, "idempotency_key": "k1"})
    h2 = idem._hash_args({"a": 1, "idempotency_key": "k2"})
    assert h1 == h2


def test_keys_isolated_per_tool():
    """Same key on different tool = miss."""
    idem.store("shared", "tool_a", {"x": 1}, {"r": "a"})
    assert idem.lookup("shared", "tool_b", {"x": 1}) == {"hit": False}


def test_expired_entries_become_miss(monkeypatch):
    """TTL: entries older than DEFAULT_TTL_S are treated as miss."""
    idem.store("k3", "tool_z", {}, {"r": "stale"})
    monkeypatch.setattr(idem, "DEFAULT_TTL_S", 0.0)  # everything expired
    r = idem.lookup("k3", "tool_z", {})
    assert r["hit"] is False
    assert r["expired"] is True


# ---------- registry integration ----------

def _make_spec(fn, *, name="t1", policy_op="gmail.send"):
    """Helper to build a registry spec via the real _tool factory."""
    return registry._tool(
        name, fn, policy_op, "desc",
        {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
    )


def test_non_idempotent_tool_gets_idempotency_key_param():
    """policy_op=gmail.send is non-idempotent — schema must include the field."""
    spec = _make_spec(lambda x: {"ok": True})
    props = spec["schema"]["input_schema"]["properties"]
    assert "idempotency_key" in props
    assert spec["supports_idempotency"] is True


def test_read_only_tool_does_not_get_idempotency_key_param():
    """Reads are already idempotent — no need for the deduplication field."""
    spec = registry._tool(
        "t_read", lambda x: x, "sheets.read", "d",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )
    props = spec["schema"]["input_schema"]["properties"]
    assert "idempotency_key" not in props
    assert spec["supports_idempotency"] is False


def test_wrap_for_sdk_replays_cached_response_on_dup_key():
    """Calling the wrapped tool twice with the same key + same args runs
    `fn` exactly once and replays the cached response the second time."""
    calls = []

    def fn(x):
        calls.append(x)
        return {"x": x}

    spec = _make_spec(fn)
    wrapped = registry._wrap_for_sdk(spec)
    # SDK-wrapped tools expose the inner handler at `.handler`
    handler = wrapped.handler

    args1 = {"x": 7, "idempotency_key": "key-alpha"}
    args2 = {"x": 7, "idempotency_key": "key-alpha"}

    r1 = asyncio.run(handler(args1))
    r2 = asyncio.run(handler(args2))

    assert len(calls) == 1, "second call must hit cache, not execute fn"
    assert r1 == r2
    assert json.loads(r1["content"][0]["text"])["x"] == 7


def test_wrap_for_sdk_no_cache_when_key_missing():
    """No key → tool runs every call (preserves existing behavior)."""
    calls = []

    def fn(x):
        calls.append(x)
        return {"x": x}

    spec = _make_spec(fn)
    wrapped = registry._wrap_for_sdk(spec)
    handler = wrapped.handler

    asyncio.run(handler({"x": 1}))
    asyncio.run(handler({"x": 1}))
    assert len(calls) == 2


def test_wrap_for_sdk_mismatch_returns_error():
    """Same key + different args → idempotency_conflict, not silent overwrite."""
    spec = _make_spec(lambda x: {"x": x})
    wrapped = registry._wrap_for_sdk(spec)
    handler = wrapped.handler

    asyncio.run(handler({"x": 1, "idempotency_key": "kc"}))
    r2 = asyncio.run(handler({"x": 2, "idempotency_key": "kc"}))

    assert r2["is_error"] is True
    body = json.loads(r2["content"][0]["text"])
    assert body["_meta"]["error_kind"] == "idempotency_conflict"


def test_wrap_for_sdk_does_not_cache_errors():
    """A failed call must NOT be cached — retrying with the same key should
    re-attempt the tool, not replay the error."""
    state = {"calls": 0}

    def fn(x):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient")
        return {"x": x}

    spec = _make_spec(fn)
    wrapped = registry._wrap_for_sdk(spec)
    handler = wrapped.handler

    r1 = asyncio.run(handler({"x": 1, "idempotency_key": "retry-key"}))
    r2 = asyncio.run(handler({"x": 1, "idempotency_key": "retry-key"}))

    assert r1.get("is_error") is True
    assert r2.get("is_error") is not True
    assert state["calls"] == 2
