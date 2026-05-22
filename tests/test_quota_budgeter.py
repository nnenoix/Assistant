"""Unit tests for src/tools/_quota.py (Phase 14A-prep)."""
import time

import pytest

from src.tools import _quota


@pytest.fixture(autouse=True)
def _reset_budgeter():
    """Each test starts with a clean budgeter to avoid bleed-through."""
    _quota.reset()
    yield
    _quota.reset()


def test_no_pacing_below_limit():
    """First N calls (N < bucket limit) should not pace."""
    bucket = "sheets-direct"
    limit, _ = _quota.BUCKETS[bucket]
    for _ in range(limit - 5):
        assert _quota.acquire(bucket) == 0.0


def test_apps_script_never_paces():
    """Apps Script bucket is exempt — 1 token per call regardless of N books."""
    for _ in range(1000):
        assert _quota.acquire("apps-script") == 0.0


def test_unknown_bucket_returns_zero():
    """Unconfigured bucket should be a no-op (don't crash on typos)."""
    assert _quota.acquire("nonexistent-bucket") == 0.0


def test_paces_when_window_full(monkeypatch):
    """Once limit is reached, next acquire must sleep until oldest entry exits window."""
    bucket = "drive"  # smaller limit (20)
    limit, window_s = _quota.BUCKETS[bucket]

    # Fake time so we don't actually wait
    fake_now = [1000.0]
    sleep_calls = []

    def fake_time():
        return fake_now[0]

    def fake_sleep(s):
        sleep_calls.append(s)
        fake_now[0] += s  # advance time

    monkeypatch.setattr(_quota.time, "time", fake_time)
    monkeypatch.setattr(_quota.time, "sleep", fake_sleep)

    # Fill the window
    for _ in range(limit):
        assert _quota.acquire(bucket) == 0.0
    # Next call should pace
    paced = _quota.acquire(bucket)
    assert paced > 0.0
    assert sleep_calls  # we did sleep
    # The pacing should target the window edge — slept just under window_s
    assert paced <= window_s * 1000 + 1


def test_buckets_isolated():
    """Filling one bucket must not affect another."""
    bucket_a = "drive"  # limit 20
    bucket_b = "sheets-direct"  # limit 50
    limit_a, _ = _quota.BUCKETS[bucket_a]
    for _ in range(limit_a):
        _quota.acquire(bucket_a)
    # bucket_b is unaffected — should not pace
    assert _quota.acquire(bucket_b) == 0.0


def test_remaining_pct_basic():
    bucket = "drive"
    limit, _ = _quota.BUCKETS[bucket]
    assert _quota.remaining_pct(bucket) == 1.0
    for _ in range(limit // 2):
        _quota.acquire(bucket)
    pct = _quota.remaining_pct(bucket)
    assert 0.4 <= pct <= 0.6


def test_remaining_pct_exempt_bucket_returns_none():
    assert _quota.remaining_pct("apps-script") is None
    assert _quota.remaining_pct("nonexistent") is None


def test_remaining_pct_zero_when_full():
    bucket = "drive"
    limit, _ = _quota.BUCKETS[bucket]
    for _ in range(limit):
        _quota.acquire(bucket)
    assert _quota.remaining_pct(bucket) == 0.0


def test_window_slides_old_entries_drop(monkeypatch):
    """After window_s elapses, old entries should drop and pacing relaxes."""
    bucket = "drive"
    limit, window_s = _quota.BUCKETS[bucket]
    fake_now = [1000.0]
    monkeypatch.setattr(_quota.time, "time", lambda: fake_now[0])

    # Fill the bucket
    for _ in range(limit):
        _quota.acquire(bucket)
    assert _quota.remaining_pct(bucket) == 0.0

    # Advance time past window
    fake_now[0] += window_s + 1
    # Now the window is empty again
    assert _quota.remaining_pct(bucket) == 1.0
    assert _quota.acquire(bucket) == 0.0


def test_reset_clears_logs():
    bucket = "drive"
    for _ in range(5):
        _quota.acquire(bucket)
    assert _quota.remaining_pct(bucket) < 1.0
    _quota.reset(bucket)
    assert _quota.remaining_pct(bucket) == 1.0


def test_reset_all_clears_all_buckets():
    for b in ("drive", "sheets-direct", "gmail"):
        _quota.acquire(b)
    _quota.reset()
    for b in ("drive", "sheets-direct", "gmail"):
        assert _quota.remaining_pct(b) == 1.0


def test_thread_safety_smoke():
    """Concurrent acquires should not crash or produce negative pct."""
    import threading
    bucket = "sheets-direct"
    limit, _ = _quota.BUCKETS[bucket]
    errors = []

    def worker():
        try:
            for _ in range(10):
                _quota.acquire(bucket)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    # remaining_pct should be in [0.0, 1.0]
    pct = _quota.remaining_pct(bucket)
    assert 0.0 <= pct <= 1.0
