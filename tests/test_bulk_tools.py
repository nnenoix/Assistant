"""Unit tests for Phase 14 bulk tools (sheets_bulk_metric, bulk_load_results).

Phase 14A: parallel cell-read across N spreadsheets with mandatory cell hint,
payload compaction, dry_run preview.
"""
import json
from unittest.mock import patch

import pytest


# ============ sheets_bulk_metric ============

def test_bulk_metric_basic():
    """Reads same cell from 3 books in parallel."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[{"a": 100, "b": 200, "c": 300}[sid]]], "_meta": {}}

    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_metric(["a", "b", "c"], cell="Sheet1!B45")

    assert result["stats"]["n_ok"] == 3
    assert result["stats"]["n_err"] == 0
    assert result["stats"]["sum"] == 600
    assert result["stats"]["min"] == 100
    assert result["stats"]["max"] == 300
    assert result["_meta"]["cell"] == "Sheet1!B45"
    assert result["_meta"]["tool"] == "sheets_bulk_metric"
    assert result["_meta"]["result_token"].startswith("bulk_")


def test_bulk_metric_rejects_missing_cell():
    """No silent fallback — `cell` mandatory."""
    from src.tools import sheets
    with pytest.raises(ValueError, match="cell is required"):
        sheets.bulk_metric(["a", "b"], cell="")
    with pytest.raises(ValueError, match="cell is required"):
        sheets.bulk_metric(["a", "b"], cell="   ")


def test_bulk_metric_rejects_empty_ids():
    from src.tools import sheets
    with pytest.raises(ValueError, match="non-empty list"):
        sheets.bulk_metric([], cell="A1")


def test_bulk_metric_per_book_error_isolation():
    """One bad book doesn't kill the batch."""
    from src.tools import sheets

    def flaky_read(sid, range, formatted=False, account="main"):
        if sid == "bad":
            raise RuntimeError("simulated error")
        return {"values": [[42]], "_meta": {}}

    with patch("src.tools.sheets.read_range", side_effect=flaky_read):
        result = sheets.bulk_metric(["a", "bad", "b"], cell="A1")

    assert result["stats"]["n_ok"] == 2
    assert result["stats"]["n_err"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["id"] == "bad"


def test_bulk_metric_parallel_speedup():
    """ThreadPoolExecutor must give actual parallelism."""
    import time as _time
    from src.tools import sheets

    def slow_read(sid, range, formatted=False, account="main"):
        _time.sleep(0.05)  # 50ms each
        return {"values": [[1]], "_meta": {}}

    ids = [f"id{i}" for i in range(10)]
    with patch("src.tools.sheets.read_range", side_effect=slow_read):
        t0 = _time.perf_counter()
        result = sheets.bulk_metric(ids, cell="A1", max_workers=10)
        elapsed = _time.perf_counter() - t0

    # Serial would be ~0.5s; parallel 10 workers should be ~0.05s + overhead.
    # Allow generous buffer for Windows scheduler.
    assert elapsed < 0.3, f"expected <0.3s, got {elapsed:.2f}s"
    assert result["stats"]["n_ok"] == 10


def test_bulk_metric_dry_run():
    """dry_run returns estimate without executing."""
    from src.tools import sheets

    with patch("src.tools.sheets.read_range") as mock_read:
        result = sheets.bulk_metric(["a"] * 200, cell="A1", dry_run=True)

    assert mock_read.call_count == 0  # NOT executed
    assert result["_meta"]["dry_run"] is True
    assert result["estimated_api_calls"] == 200
    assert result["estimated_quota_pressure"] == "high"
    assert "cross_aggregate" in result["recommendation"]


def test_bulk_metric_dry_run_recommendation_thresholds():
    from src.tools import sheets

    small = sheets.bulk_metric(["a"] * 10, cell="A1", dry_run=True)
    medium = sheets.bulk_metric(["a"] * 60, cell="A1", dry_run=True)
    large = sheets.bulk_metric(["a"] * 200, cell="A1", dry_run=True)

    assert small["estimated_quota_pressure"] == "ok"
    assert small["recommendation"] is None
    assert medium["estimated_quota_pressure"] == "medium"
    assert medium["recommendation"] is not None
    assert large["estimated_quota_pressure"] == "high"


def test_bulk_metric_max_workers_clamped():
    """max_workers clamped to [1, 16] and to ref count."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[1]], "_meta": {}}

    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_metric(["a", "b"], cell="A1", max_workers=999)
    assert result["_meta"]["max_workers"] == 2  # clamped to len(ids)

    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_metric(["a"] * 30, cell="A1", max_workers=999)
    assert result["_meta"]["max_workers"] == 16  # clamped to _MAX_BULK_WORKERS


def test_bulk_metric_outliers_top_and_bottom():
    """Outliers are top 10 + bottom 10 by value."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        # sid is "id0", "id1", ... — value = int(sid[2:])
        return {"values": [[int(sid[2:])]], "_meta": {}}

    ids = [f"id{i:03d}" for i in range(50)]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_metric(ids, cell="A1")

    assert len(result["outliers"]["top"]) == 10
    assert result["outliers"]["top"][0]["value"] == 49
    assert result["outliers"]["bottom"][0]["value"] == 0


def test_bulk_metric_compacted_payload_under_12k_for_500():
    """500 books → final JSON payload must fit MAX_TOOL_PAYLOAD = 12 000."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[hash(sid) % 1_000_000]], "_meta": {}}

    # Drive-like 44-char IDs to be realistic
    ids = [f"1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_ABc_{i:03d}" for i in range(500)]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_metric(ids, cell="Год факт!B45")

    payload = json.dumps(result, ensure_ascii=False, default=str)
    assert len(payload) < 12_000, f"payload {len(payload)} chars exceeds MAX_TOOL_PAYLOAD"


def test_bulk_metric_full_data_via_load_results():
    """bulk_load_results returns full per-item data."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[int(sid[2:]) * 10]], "_meta": {}}

    ids = [f"id{i:03d}" for i in range(100)]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        compacted = sheets.bulk_metric(ids, cell="A1")

    full = sheets.bulk_load_results(compacted["_meta"]["result_token"])
    assert len(full["items"]) == 100
    assert full["op"] == "sum"
    # Sanity check on values
    by_id = {it["id"]: it["value"] for it in full["items"]}
    assert by_id["id042"] == 420


def test_bulk_metric_empty_cell_handling():
    """Books returning empty cells should be counted as n_ok (with None value),
    not n_err. compute_stats treats None as n_err in its own count, which is
    correct — they're 'usable reads, just empty values'."""
    from src.tools import sheets

    def empty_read(sid, range, formatted=False, account="main"):
        return {"values": [], "_meta": {"empty_reason": "no_data"}}

    with patch("src.tools.sheets.read_range", side_effect=empty_read):
        result = sheets.bulk_metric(["a", "b"], cell="A1")

    # Both reads succeeded but returned None values
    assert result["stats"]["n_ok"] == 0  # No numeric values
    assert result["stats"]["n_err"] == 2  # Two None values counted as non-numeric
    # No exceptions raised — errors list stays empty
    assert result["errors"] == []


def test_bulk_metric_duration_recorded():
    from src.tools import sheets
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {"values": [[1]], "_meta": {}}
        result = sheets.bulk_metric(["a"], cell="A1")
    assert result["_meta"]["duration_ms"] is not None
    assert result["_meta"]["duration_ms"] >= 0


# ============ bulk_load_results ============

def test_bulk_load_results_roundtrip():
    from src.tools import sheets, _bulk_payload

    token = _bulk_payload.make_token()
    _bulk_payload.write_result_file(token, {"items": [{"id": "x", "value": 7}], "errors": [], "op": "sum"})

    full = sheets.bulk_load_results(token)
    assert full["items"] == [{"id": "x", "value": 7}]
    assert full["_meta"]["result_token"] == token
    assert full["_meta"]["total"] == 1
    assert full["_meta"]["has_more"] is False


def test_bulk_load_results_pagination():
    """500-item result split across pages of 150 — agent walks offsets."""
    from src.tools import sheets, _bulk_payload

    token = _bulk_payload.make_token()
    items = [{"id": f"id{i:03d}", "value": i * 10} for i in range(500)]
    _bulk_payload.write_result_file(token, {"items": items, "errors": [], "op": "sum"})

    # First page
    p0 = sheets.bulk_load_results(token, offset=0, limit=150)
    assert p0["_meta"]["page_size"] == 150
    assert p0["_meta"]["total"] == 500
    assert p0["_meta"]["has_more"] is True
    assert p0["_meta"]["next_offset"] == 150
    assert p0["items"][0]["id"] == "id000"
    assert p0["items"][-1]["id"] == "id149"

    # Last page
    p3 = sheets.bulk_load_results(token, offset=450, limit=150)
    assert p3["_meta"]["page_size"] == 50
    assert p3["_meta"]["has_more"] is False
    assert p3["_meta"]["next_offset"] is None
    assert p3["items"][0]["id"] == "id450"
    assert p3["items"][-1]["id"] == "id499"


def test_bulk_load_results_walk_all_pages_recovers_full_set():
    """Agent walks pages and accumulates everything."""
    from src.tools import sheets, _bulk_payload

    token = _bulk_payload.make_token()
    items = [{"id": f"id{i:03d}", "value": i} for i in range(500)]
    _bulk_payload.write_result_file(token, {"items": items, "errors": [], "op": "sum"})

    collected = []
    offset = 0
    while True:
        page = sheets.bulk_load_results(token, offset=offset, limit=150)
        collected.extend(page["items"])
        if not page["_meta"]["has_more"]:
            break
        offset = page["_meta"]["next_offset"]

    assert len(collected) == 500
    assert collected[0]["id"] == "id000"
    assert collected[-1]["id"] == "id499"


def test_bulk_load_results_missing_token():
    from src.tools import sheets
    with pytest.raises(FileNotFoundError):
        sheets.bulk_load_results("bulk_0_doesnotexist")


def test_bulk_load_results_rejects_unsafe_token():
    from src.tools import sheets
    with pytest.raises(ValueError):
        sheets.bulk_load_results("../../etc/passwd")


# ============ registration ============

def test_bulk_metric_registered():
    from src.tools import registry
    names = {t["name"] for t in registry.TOOLS}
    assert "sheets_bulk_metric" in names
    assert "bulk_load_results" in names


def test_bulk_metric_schema_requires_cell():
    from src.tools import registry
    spec = next(t for t in registry.TOOLS if t["name"] == "sheets_bulk_metric")
    assert "cell" in spec["schema"]["input_schema"]["required"]
    assert "spreadsheet_ids" in spec["schema"]["input_schema"]["required"]


# ============ sheets_bulk_read ============

def test_bulk_read_basic_scalar_refs():
    """100 single-cell refs across different books, stats meaningful."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        # range e.g. "Sheet1!A1" → return distinct value per (sid, range)
        return {"values": [[hash((sid, range)) % 1000]], "_meta": {"range_read": range}}

    refs = [{"spreadsheet_id": f"book{i}", "range": "A1"} for i in range(20)]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_read(refs)

    assert result["stats"]["n_ok"] == 20
    assert result["stats"]["n_err"] == 0
    assert result["_meta"]["op"] == "read"
    assert result["_meta"]["tool"] == "sheets_bulk_read"
    # All scalar reads → outliers populated
    assert len(result["outliers"]["top"]) > 0


def test_bulk_read_grid_refs_no_scalar_stats():
    """Multi-cell ranges → value=None per ref; stats degrade gracefully."""
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[1, 2, 3], [4, 5, 6]], "_meta": {"range_read": range}}

    refs = [{"spreadsheet_id": f"book{i}", "range": "A1:C2"} for i in range(5)]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_read(refs)

    # All 5 reads succeeded but value=None (because not 1x1)
    assert result["stats"]["n_ok"] == 0
    assert result["stats"]["n_err"] == 5  # non-numeric (None)
    assert result["errors"] == []  # no exceptions


def test_bulk_read_per_ref_dims_preserved():
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[1, 2], [3, 4], [5, 6]], "_meta": {"range_read": range}}

    refs = [{"spreadsheet_id": "book1", "range": "A1:B3"}]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_read(refs)

    full = sheets.bulk_load_results(result["_meta"]["result_token"])
    assert full["items"][0]["dims"] == [3, 2]
    assert full["items"][0]["values"] == [[1, 2], [3, 4], [5, 6]]


def test_bulk_read_rejects_invalid_ref_shape():
    from src.tools import sheets

    with pytest.raises(ValueError, match="non-empty"):
        sheets.bulk_read([])
    with pytest.raises(ValueError, match="dict"):
        sheets.bulk_read(["not a dict"])
    with pytest.raises(ValueError, match="spreadsheet_id"):
        sheets.bulk_read([{"range": "A1"}])  # missing spreadsheet_id


def test_bulk_read_per_ref_error_isolation():
    from src.tools import sheets

    def flaky(sid, range, formatted=False, account="main"):
        if sid == "bad":
            raise RuntimeError("simulated permission denied")
        return {"values": [[1]], "_meta": {}}

    refs = [
        {"spreadsheet_id": "ok1", "range": "A1"},
        {"spreadsheet_id": "bad", "range": "A1"},
        {"spreadsheet_id": "ok2", "range": "A1"},
    ]
    with patch("src.tools.sheets.read_range", side_effect=flaky):
        result = sheets.bulk_read(refs)

    assert result["stats"]["n_ok"] == 2
    assert len(result["errors"]) == 1
    assert "bad:A1" in result["errors"][0]["id"]


def test_bulk_read_per_ref_formatted_override():
    """formatted is per-ref-overridable from top-level default."""
    from src.tools import sheets

    formatted_seen = []

    def fake_read(sid, range, formatted=False, account="main"):
        formatted_seen.append((sid, formatted))
        return {"values": [[1]], "_meta": {}}

    refs = [
        {"spreadsheet_id": "a", "range": "A1"},  # uses top-level default
        {"spreadsheet_id": "b", "range": "A1", "formatted": True},  # overrides
    ]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        sheets.bulk_read(refs, formatted=False)

    by_sid = dict(formatted_seen)
    assert by_sid["a"] is False
    assert by_sid["b"] is True


def test_bulk_read_dry_run():
    from src.tools import sheets

    refs = [{"spreadsheet_id": f"id{i}", "range": "A1"} for i in range(75)]
    with patch("src.tools.sheets.read_range") as mock_read:
        result = sheets.bulk_read(refs, dry_run=True)

    assert mock_read.call_count == 0
    assert result["_meta"]["dry_run"] is True
    assert result["estimated_api_calls"] == 75
    assert result["estimated_quota_pressure"] == "medium"


def test_bulk_read_registered():
    from src.tools import registry
    names = {t["name"] for t in registry.TOOLS}
    assert "sheets_bulk_read" in names


def test_bulk_read_compacted_under_12k_for_500_refs():
    from src.tools import sheets

    def fake_read(sid, range, formatted=False, account="main"):
        return {"values": [[hash(sid) % 999]], "_meta": {"range_read": range}}

    refs = [
        {"spreadsheet_id": f"1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_ABc_{i:03d}",
         "range": "Год факт!B45"}
        for i in range(500)
    ]
    with patch("src.tools.sheets.read_range", side_effect=fake_read):
        result = sheets.bulk_read(refs)

    payload = json.dumps(result, ensure_ascii=False, default=str)
    assert len(payload) < 12_000
