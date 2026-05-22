"""Unit tests for src/tools/_bulk_payload.py (Phase 14A-prep)."""
import json

import pytest

from src.tools import _bulk_payload as bp


# ---------- compute_stats ----------

def test_compute_stats_basic():
    s = bp.compute_stats([10, 20, 30, 40, 50])
    assert s["n_ok"] == 5
    assert s["n_err"] == 0
    assert s["sum"] == 150
    assert s["mean"] == 30
    assert s["p50"] == 30
    assert s["min"] == 10
    assert s["max"] == 50


def test_compute_stats_skips_non_numeric():
    s = bp.compute_stats([10, None, "abc", 20, float("nan"), 30])
    # 10, 20, 30 are numeric; rest counted as n_err
    assert s["n_ok"] == 3
    assert s["n_err"] == 3
    assert s["sum"] == 60
    assert s["min"] == 10
    assert s["max"] == 30


def test_compute_stats_all_none():
    s = bp.compute_stats([None, None, None])
    assert s["n_ok"] == 0
    assert s["n_err"] == 3
    assert s["sum"] is None
    assert s["mean"] is None
    assert s["p50"] is None


def test_compute_stats_empty():
    s = bp.compute_stats([])
    assert s["n_ok"] == 0
    assert s["n_err"] == 0
    assert s["sum"] is None


def test_compute_stats_single_value():
    s = bp.compute_stats([42])
    assert s["n_ok"] == 1
    assert s["sum"] == 42
    assert s["mean"] == 42
    assert s["p50"] == 42
    assert s["p95"] == 42
    assert s["min"] == 42
    assert s["max"] == 42


def test_compute_stats_bools_excluded():
    """Python: isinstance(True, int) == True. Exclude bools from numeric stats."""
    s = bp.compute_stats([True, False, 10, 20])
    assert s["n_ok"] == 2
    assert s["sum"] == 30
    assert s["n_err"] == 2


# ---------- compute_outliers ----------

def test_compute_outliers_numeric_op_returns_top_and_bottom():
    items = [{"id": f"id{i}", "value": i} for i in range(1, 21)]  # 1..20
    out = bp.compute_outliers(items, op="sum", k=3)
    assert [x["value"] for x in out["top"]] == [20, 19, 18]
    assert [x["value"] for x in out["bottom"]] == [1, 2, 3]


def test_compute_outliers_list_op_returns_empty():
    items = [{"id": "a", "value": 1}, {"id": "b", "value": 2}]
    out = bp.compute_outliers(items, op="list")
    assert out == {"top": [], "bottom": []}


def test_compute_outliers_count_op_returns_empty():
    items = [{"id": "a", "value": 5}]
    out = bp.compute_outliers(items, op="count")
    assert out == {"top": [], "bottom": []}


def test_compute_outliers_truncated_at_k():
    items = [{"id": f"id{i}", "value": i} for i in range(50)]
    out = bp.compute_outliers(items, op="sum", k=10)
    assert len(out["top"]) == 10
    assert len(out["bottom"]) == 10


def test_compute_outliers_skips_non_numeric():
    items = [{"id": "a", "value": None}, {"id": "b", "value": 5},
             {"id": "c", "value": "x"}, {"id": "d", "value": 10}]
    out = bp.compute_outliers(items, op="sum", k=5)
    vals = {x["value"] for x in out["top"]} | {x["value"] for x in out["bottom"]}
    assert vals == {5, 10}


# ---------- file roundtrip ----------

def test_write_and_load_roundtrip():
    token = bp.make_token()
    payload = {"items": [{"id": "a", "value": 1}], "errors": [], "op": "sum"}
    path = bp.write_result_file(token, payload)
    assert path.exists()
    back = bp.load_result_file(token)
    assert back == payload


def test_load_unknown_token_raises():
    with pytest.raises(FileNotFoundError):
        bp.load_result_file("bulk_0_nonexistent")


def test_load_rejects_unsafe_token():
    with pytest.raises(ValueError):
        bp.load_result_file("../etc/passwd")
    with pytest.raises(ValueError):
        bp.load_result_file("bulk_../sneaky")


# ---------- cleanup ----------

def test_cleanup_old_keeps_at_most_max(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "BULK_DIR", tmp_path)
    # Create 5 files; cleanup to keep 2
    for i in range(5):
        p = tmp_path / f"bulk_{1000+i}_aaaa.json"
        p.write_text('{}', encoding="utf-8")
    deleted = bp.cleanup_old(max_keep=2)
    remaining = sorted(tmp_path.glob("bulk_*.json"))
    assert deleted == 3
    assert len(remaining) == 2


# ---------- compact ----------

def test_compact_basic_shape():
    items = [{"id": "a", "value": 100}, {"id": "b", "value": 200}]
    result = bp.compact(items, op="sum")
    assert "stats" in result
    assert "outliers" in result
    assert "errors" in result
    assert result["_meta"]["n"] == 2
    assert result["_meta"]["op"] == "sum"
    assert result["_meta"]["result_token"].startswith("bulk_")
    assert result["stats"]["sum"] == 300


def test_compact_marks_truncated_when_errors_present():
    """Existing _meta_warning_prefix fires on truncated=True — re-use it."""
    items = [{"id": "a", "value": 1}]
    errors = [{"id": "b", "kind": "PermissionDenied", "msg": "no access"}]
    result = bp.compact(items, op="sum", errors=errors)
    assert result["_meta"]["truncated"] is True
    assert "errors" in result["_meta"]["truncation_reason"]
    assert result["stats"]["n_err"] == 1


def test_compact_truncates_errors_list_at_5():
    items = []
    errors = [{"id": f"e{i}", "kind": "X", "msg": str(i)} for i in range(20)]
    result = bp.compact(items, op="sum", errors=errors)
    assert len(result["errors"]) == 5
    # But stats.n_err counts ALL errors
    assert result["stats"]["n_err"] == 20


def test_compact_fits_under_max_tool_payload_for_500_items():
    """Critical: 500-book result must fit in 12k payload budget."""
    items = [
        {"id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_AbCdE_" + str(i).zfill(3),
         "value": 1000000 + i * 137}
        for i in range(500)
    ]
    result = bp.compact(items, op="sum")
    payload = json.dumps(result, ensure_ascii=False, default=str)
    assert len(payload) < 12_000, f"compact payload {len(payload)} chars exceeds MAX_TOOL_PAYLOAD"
    # And outliers should be capped at MAX_OUTLIERS_PER_TAIL
    assert len(result["outliers"]["top"]) <= bp.MAX_OUTLIERS_PER_TAIL
    assert len(result["outliers"]["bottom"]) <= bp.MAX_OUTLIERS_PER_TAIL


def test_compact_full_data_recoverable_via_token():
    items = [{"id": f"id{i}", "value": i} for i in range(500)]
    errors = [{"id": "bad", "kind": "X", "msg": "err"}]
    result = bp.compact(items, op="sum", errors=errors)
    token = result["_meta"]["result_token"]
    full = bp.load_result_file(token)
    assert len(full["items"]) == 500
    assert len(full["errors"]) == 1


def test_compact_duration_ms_when_started_at_given():
    import time as _time
    t0 = _time.perf_counter()
    _time.sleep(0.01)  # ensure non-zero
    result = bp.compact([{"id": "a", "value": 1}], op="sum", started_at=t0)
    assert result["_meta"]["duration_ms"] is not None
    assert result["_meta"]["duration_ms"] >= 5.0
