"""Integration tests for QuotaBudgeter wired into _wrap_for_sdk (Phase 14F)."""
import asyncio
import json

import pytest

from src.tools import _quota
from src.tools.registry import _wrap_for_sdk, _bucket_for_policy_op


@pytest.fixture(autouse=True)
def _reset_budgeter():
    _quota.reset()
    yield
    _quota.reset()


def _run(wrapped, args):
    """Invoke a wrapped tool from a sync test."""
    handler = getattr(wrapped, "handler", wrapped)
    return asyncio.run(handler(args))


# ---------- policy_op → bucket mapping ----------

def test_bucket_for_sheets_ops():
    assert _bucket_for_policy_op("sheets.read") == "sheets-direct"
    assert _bucket_for_policy_op("sheets.write") == "sheets-direct"


def test_bucket_for_drive_ops():
    assert _bucket_for_policy_op("drive.read") == "drive"
    assert _bucket_for_policy_op("drive.write") == "drive"


def test_bucket_for_gmail_ops():
    assert _bucket_for_policy_op("gmail.read") == "gmail"
    assert _bucket_for_policy_op("gmail.modify") == "gmail"


def test_bucket_for_apps_script_ops():
    assert _bucket_for_policy_op("apps_script.run") == "apps-script"


def test_bucket_none_for_unbudgeted_ops():
    """Verify / self / calendar / docs / slides etc. are not budgeted."""
    assert _bucket_for_policy_op("verify.read") is None
    assert _bucket_for_policy_op("self.test") is None
    assert _bucket_for_policy_op("calendar.read") is None
    assert _bucket_for_policy_op("docs.write") is None
    assert _bucket_for_policy_op(None) is None
    assert _bucket_for_policy_op("") is None


# ---------- _wrap_for_sdk integration ----------

def test_no_pacing_when_below_limit_surfaces_remaining_pct():
    """Tools with a budgeted policy_op get _meta.quota_remaining_pct surfaced."""
    def fn():
        return {"values": [[1]], "_meta": {"range_read": "A1"}}

    spec = {
        "name": "fake_sheets_read",
        "fn": fn,
        "policy_op": "sheets.read",
        "category": "sheets",
        "schema": {"name": "fake_sheets_read", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    result = _run(wrapped, {})

    parsed = json.loads(result["content"][0]["text"])
    # Below limit → no pacing
    assert "quota_paced_ms" not in parsed["_meta"]
    # But remaining_pct is always surfaced for budgeted tools
    assert "quota_remaining_pct" in parsed["_meta"]
    assert 0.0 <= parsed["_meta"]["quota_remaining_pct"] <= 1.0


def test_apps_script_bucket_never_paces():
    """apps-script bucket is exempt from pacing (1 token per call)."""
    def fn():
        return {"value": 42, "_meta": {}}

    spec = {
        "name": "fake_apps_script",
        "fn": fn,
        "policy_op": "apps_script.run",
        "category": "apps_script",
        "schema": {"name": "fake_apps_script", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    # Spam it many times — no pacing should ever apply
    for _ in range(50):
        result = _run(wrapped, {})
    parsed = json.loads(result["content"][0]["text"])
    assert "quota_paced_ms" not in parsed["_meta"]
    # remaining_pct is None for exempt buckets → not surfaced
    assert "quota_remaining_pct" not in parsed["_meta"]


def test_no_budgeting_for_unconfigured_policy_op():
    """verify.* / self.* tools have NO quota fields added."""
    def fn():
        return {"verdict": "ok", "_meta": {"ref_count": 0}}

    spec = {
        "name": "fake_verify",
        "fn": fn,
        "policy_op": "verify.read",
        "category": "verify",
        "schema": {"name": "fake_verify", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    result = _run(wrapped, {})
    parsed = json.loads(result["content"][0]["text"])
    assert "quota_paced_ms" not in parsed["_meta"]
    assert "quota_remaining_pct" not in parsed["_meta"]


def test_pacing_triggers_when_window_full(monkeypatch):
    """Once bucket is saturated, next call sleeps; _meta.quota_paced_ms reports it."""
    bucket = "drive"
    limit, _ = _quota.BUCKETS[bucket]

    # Make sleeps fast so test stays quick
    fake_now = [10000.0]
    sleep_calls = []
    monkeypatch.setattr(_quota.time, "time", lambda: fake_now[0])
    def fake_sleep(s):
        sleep_calls.append(s)
        fake_now[0] += s
    monkeypatch.setattr(_quota.time, "sleep", fake_sleep)

    # Pre-fill the bucket
    for _ in range(limit):
        _quota.acquire(bucket)

    def fn():
        return {"files": [], "_meta": {"count": 0}}

    spec = {
        "name": "fake_drive_search",
        "fn": fn,
        "policy_op": "drive.read",
        "category": "drive",
        "schema": {"name": "fake_drive_search", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    result = _run(wrapped, {})
    parsed = json.loads(result["content"][0]["text"])
    assert parsed["_meta"]["quota_paced_ms"] > 0
    assert sleep_calls  # We did sleep


def test_quota_fields_not_added_when_no_meta_dict():
    """Tools returning non-dict or dict-without-_meta keep their shape."""
    def fn():
        return "just a string"

    spec = {
        "name": "fake_string_returner",
        "fn": fn,
        "policy_op": "sheets.read",
        "category": "sheets",
        "schema": {"name": "fake_string_returner", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    result = _run(wrapped, {})
    assert "just a string" in result["content"][0]["text"]
    # No crash on missing _meta


def test_quota_fields_added_to_existing_meta():
    """Existing _meta dict is augmented, not replaced."""
    def fn():
        return {"x": 1, "_meta": {"range_read": "A1", "row_count": 1}}

    spec = {
        "name": "fake_read",
        "fn": fn,
        "policy_op": "sheets.read",
        "category": "sheets",
        "schema": {"name": "fake_read", "description": "x",
                   "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    result = _run(wrapped, {})
    parsed = json.loads(result["content"][0]["text"])
    # Original keys preserved
    assert parsed["_meta"]["range_read"] == "A1"
    assert parsed["_meta"]["row_count"] == 1
    # Budget signal added
    assert "quota_remaining_pct" in parsed["_meta"]
