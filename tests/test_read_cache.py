"""Unit tests for src/tools/_read_cache.py + integration into sheets.read_range
and sheets.write_and_verify cache invalidation (Phase 14E)."""
import os
import time as _time
from unittest.mock import patch, MagicMock

import pytest

from src.tools import _read_cache


@pytest.fixture(autouse=True)
def _reset_and_enable():
    """Each test starts with a fresh, ENABLED cache so we can assert hits."""
    _read_cache.CACHE.clear()
    _read_cache.CACHE.enable()
    yield
    _read_cache.CACHE.clear()
    _read_cache.CACHE.disable()


# ---------- core cache mechanics ----------

def test_cache_disabled_by_default(monkeypatch):
    """Without SHEETS_READ_CACHE=1, the singleton stays disabled at import."""
    monkeypatch.delenv("SHEETS_READ_CACHE", raising=False)
    fresh = _read_cache.SheetReadCache()
    assert fresh.enabled is False
    # set() and get() are no-ops
    fresh.set(("a", "b", "c", False), "value")
    assert fresh.get(("a", "b", "c", False)) is None
    assert fresh.size() == 0


def test_cache_enabled_via_env(monkeypatch):
    monkeypatch.setenv("SHEETS_READ_CACHE", "1")
    fresh = _read_cache.SheetReadCache()
    assert fresh.enabled is True


def test_set_and_get_hit():
    key = ("acct", "sid", "A1", False)
    _read_cache.CACHE.set(key, {"values": [[1]], "_meta": {}})
    got = _read_cache.CACHE.get(key)
    assert got == {"values": [[1]], "_meta": {}}


def test_get_miss_returns_none():
    assert _read_cache.CACHE.get(("acct", "sid", "Z99", False)) is None


def test_ttl_expiration():
    """After TTL, entries vanish from get()."""
    cache = _read_cache.SheetReadCache(ttl_s=0.05)
    cache.enable()
    cache.set(("a", "b", "c", False), "v")
    assert cache.get(("a", "b", "c", False)) == "v"
    _time.sleep(0.1)
    assert cache.get(("a", "b", "c", False)) is None


def test_lru_eviction():
    """When over capacity, oldest unused entry is evicted."""
    cache = _read_cache.SheetReadCache(max_entries=3)
    cache.enable()
    for i in range(5):
        cache.set((f"k{i}",), f"v{i}")
    assert cache.size() == 3
    # Oldest two (k0, k1) should be gone; k2/k3/k4 retained
    assert cache.get(("k0",)) is None
    assert cache.get(("k4",)) == "v4"


def test_lru_move_to_end_on_hit():
    """Accessing an entry refreshes its LRU position."""
    cache = _read_cache.SheetReadCache(max_entries=3)
    cache.enable()
    cache.set(("k0",), "v0")
    cache.set(("k1",), "v1")
    cache.set(("k2",), "v2")
    # Touch k0 → it becomes most-recent
    assert cache.get(("k0",)) == "v0"
    # Add k3 → k1 should be evicted (not k0)
    cache.set(("k3",), "v3")
    assert cache.get(("k0",)) == "v0"
    assert cache.get(("k1",)) is None
    assert cache.get(("k3",)) == "v3"


def test_invalidate_by_spreadsheet():
    _read_cache.CACHE.set(("a", "SID1", "A1", False), "v1")
    _read_cache.CACHE.set(("a", "SID1", "B2", False), "v2")
    _read_cache.CACHE.set(("a", "SID2", "A1", False), "v3")
    n = _read_cache.invalidate("SID1")
    assert n == 2
    assert _read_cache.CACHE.get(("a", "SID1", "A1", False)) is None
    assert _read_cache.CACHE.get(("a", "SID2", "A1", False)) == "v3"


def test_clear_drops_everything():
    _read_cache.CACHE.set(("a",), "v")
    _read_cache.CACHE.set(("b",), "v")
    n = _read_cache.CACHE.clear()
    assert n == 2
    assert _read_cache.CACHE.size() == 0


def test_make_key_distinguishes_formatted():
    raw = _read_cache.make_key("main", "SID", "A1", False)
    fmt = _read_cache.make_key("main", "SID", "A1", True)
    assert raw != fmt


def test_make_key_distinguishes_accounts():
    a = _read_cache.make_key("main", "SID", "A1", False)
    b = _read_cache.make_key("other", "SID", "A1", False)
    assert a != b


# ---------- integration with sheets.read_range ----------

def test_read_range_hits_cache_on_second_call():
    from src.tools import sheets

    mock_resp = {"range": "A1", "values": [[42]]}
    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = mock_resp

        r1 = sheets.read_range("SID", "A1")
        r2 = sheets.read_range("SID", "A1")

        # API called once (first call only)
        assert mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.call_count == 1
        # Second call signals cache hit
        assert r1["_meta"].get("from_cache") is not True
        assert r2["_meta"]["from_cache"] is True
        # Values match
        assert r1["values"] == r2["values"] == [[42]]


def test_read_range_cache_keyed_per_range():
    """Different ranges in the same book are separate entries."""
    from src.tools import sheets

    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"range": "A1", "values": [[1]]},
            {"range": "B2", "values": [[2]]},
        ]

        r1 = sheets.read_range("SID", "A1")
        r2 = sheets.read_range("SID", "B2")
        assert r1["values"] == [[1]]
        assert r2["values"] == [[2]]
        # No false cache hits
        assert r1["_meta"].get("from_cache") is not True
        assert r2["_meta"].get("from_cache") is not True


def test_read_range_no_cache_when_disabled():
    _read_cache.CACHE.disable()
    from src.tools import sheets

    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "range": "A1", "values": [[1]],
        }
        sheets.read_range("SID", "A1")
        sheets.read_range("SID", "A1")
        # API called twice (no cache)
        assert mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.call_count == 2


def test_read_named_range_uses_cache():
    from src.tools import sheets

    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "range": "Sheet1!B45", "values": [[3087967]],
        }
        r1 = sheets.read_named_range("SID", "ChistayaPribyl")
        r2 = sheets.read_named_range("SID", "ChistayaPribyl")
        assert r2["_meta"]["from_cache"] is True


def test_read_named_range_key_isolated_from_range():
    """A named range 'A1' must not collide with a literal 'A1' range read."""
    from src.tools import sheets

    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = [
            {"range": "Sheet1!A1", "values": [[999]]},   # literal A1
            {"range": "Sheet1!B45", "values": [[42]]},   # named "A1" → resolves to B45
        ]
        r_literal = sheets.read_range("SID", "A1")
        r_named = sheets.read_named_range("SID", "A1")
        assert r_literal["values"] == [[999]]
        assert r_named["values"] == [[42]]
        # No collision — both are MISSES
        assert r_literal["_meta"].get("from_cache") is not True
        assert r_named["_meta"].get("from_cache") is not True


def test_write_and_verify_invalidates_cache():
    """After write_and_verify, cached reads of that spreadsheet are dropped.

    Invalidation now lives inside write_range (bug_015 fix), so we mock at the
    _service level rather than stubbing write_range — otherwise the test would
    skip the very call site it's supposed to exercise.
    """
    from src.tools import sheets

    _read_cache.CACHE.set(
        _read_cache.make_key("main", "SID", "A1", False),
        {"values": [[1]], "_meta": {}},
    )
    assert _read_cache.CACHE.size() == 1

    with patch.object(sheets, "snapshot_range") as ms, \
         patch.object(sheets, "_snapshot", return_value=None), \
         patch.object(sheets, "_service") as mock_svc:
        ms.return_value = {"values": [[1]]}
        # write (inside write_range): update.execute() returns whatever
        mock_svc.return_value.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
        # read-back: returns the new value
        mock_svc.return_value.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "range": "A1", "values": [[2]],
        }
        sheets.write_and_verify("SID", "A1", [[2]])

    # Stale seed must be gone. The read-back inside write_and_verify re-populates
    # the cache with the fresh value [[2]], so we don't assert == None.
    cached = _read_cache.CACHE.get(_read_cache.make_key("main", "SID", "A1", False))
    assert cached is None or cached.get("values") == [[2]]
    assert cached is None or cached.get("values") != [[1]]


# ---------- bug_015: every mutating sheets fn must invalidate cache ----------
# Regression lockdown — bare write_range / append_rows / clear_range / rollback
# / find_and_replace previously left stale cache entries when SHEETS_READ_CACHE=1.

def _seed(sid: str = "SID") -> tuple:
    key = _read_cache.make_key("main", sid, "A1", False)
    _read_cache.CACHE.set(key, {"values": [[1]], "_meta": {}})
    assert _read_cache.CACHE.get(key) == {"values": [[1]], "_meta": {}}
    return key


def test_write_range_invalidates_cache():
    from src.tools import sheets

    key = _seed()
    with patch.object(sheets, "_service") as mock_svc, \
         patch.object(sheets, "_snapshot", return_value=None):
        mock_svc.return_value.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
        sheets.write_range("SID", "A1", [[2]])
    assert _read_cache.CACHE.get(key) is None


def test_append_rows_invalidates_cache():
    from src.tools import sheets

    key = _seed()
    with patch.object(sheets, "_service") as mock_svc:
        mock_svc.return_value.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {}
        sheets.append_rows("SID", "A1", [[2]])
    assert _read_cache.CACHE.get(key) is None


def test_clear_range_invalidates_cache():
    from src.tools import sheets

    key = _seed()
    with patch.object(sheets, "_service") as mock_svc, \
         patch.object(sheets, "_snapshot", return_value=None):
        mock_svc.return_value.spreadsheets.return_value.values.return_value.clear.return_value.execute.return_value = {}
        sheets.clear_range("SID", "A1")
    assert _read_cache.CACHE.get(key) is None


def test_find_and_replace_invalidates_cache():
    from src.tools import sheets

    key = _seed()
    with patch.object(sheets, "_service") as mock_svc, \
         patch.object(sheets, "_snapshot", return_value=None):
        # find_and_replace without sheet=... fetches all-sheet metadata first
        mock_svc.return_value.spreadsheets.return_value.get.return_value.execute.return_value = {"sheets": []}
        mock_svc.return_value.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"findReplaceResponse": {"occurrencesChanged": 0}}],
        }
        sheets.find_and_replace("SID", "foo", "bar")
    assert _read_cache.CACHE.get(key) is None


def test_rollback_invalidates_cache(tmp_path, monkeypatch):
    from src.tools import sheets
    import json as _json

    key = _seed()
    # Point BACKUPS_DIR at a tmp dir and plant a valid snapshot file
    monkeypatch.setattr(sheets, "BACKUPS_DIR", tmp_path)
    sheet_dir = tmp_path / "SID"
    sheet_dir.mkdir()
    snap_path = sheet_dir / "snap-1.json"
    snap_path.write_text(_json.dumps({
        "snapshot_id": "snap-1",
        "spreadsheet_id": "SID",
        "account": "main",
        "range": "A1",
        "values": [[1]],
    }), encoding="utf-8")

    with patch.object(sheets, "_service") as mock_svc:
        # rollback does clear() then update()
        mock_svc.return_value.spreadsheets.return_value.values.return_value.clear.return_value.execute.return_value = {}
        mock_svc.return_value.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
        sheets.rollback("SID", "snap-1")
    assert _read_cache.CACHE.get(key) is None
