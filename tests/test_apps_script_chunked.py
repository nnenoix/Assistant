"""Unit tests for src/tools/_apps_script_chunked.py and the
sheets.cross_aggregate / cross_aggregate_status wrappers (Phase 14C).

Apps Script execution is mocked — these tests verify the Python-side
orchestration: JSON parsing of clasp output, resumption loop, status
shape conversion.
"""
import json

import pytest

from src.tools import _apps_script_chunked as chunked


# ============ parse_clasp_run_output ============

def test_parse_clean_json():
    raw = '{"status": "complete", "value": 42}'
    assert chunked.parse_clasp_run_output(raw) == {"status": "complete", "value": 42}


def test_parse_with_clasp_preamble():
    """clasp typically prints `Running function ...\\n<json>`."""
    raw = "Running function cross_aggregate...\n" + json.dumps({"status": "complete", "value": 100})
    parsed = chunked.parse_clasp_run_output(raw)
    assert parsed["value"] == 100


def test_parse_with_multiline_preamble():
    raw = (
        "Logging in...\n"
        "Loading...\n"
        "Running function cross_aggregate...\n"
        '{"status": "incomplete", "token": "abc-123", "processed_count": 50}'
    )
    parsed = chunked.parse_clasp_run_output(raw)
    assert parsed["status"] == "incomplete"
    assert parsed["token"] == "abc-123"


def test_parse_with_pretty_printed_json():
    raw = """Running function ping...
{
  "ok": true,
  "version": "phase14"
}"""
    parsed = chunked.parse_clasp_run_output(raw)
    assert parsed["ok"] is True
    assert parsed["version"] == "phase14"


def test_parse_empty_output_raises():
    with pytest.raises(ValueError, match="empty"):
        chunked.parse_clasp_run_output("")


def test_parse_non_json_raises():
    with pytest.raises(ValueError, match="could not parse"):
        chunked.parse_clasp_run_output("no json here, just text")


def test_parse_non_string_raises():
    with pytest.raises(ValueError, match="expected string"):
        chunked.parse_clasp_run_output(None)  # type: ignore[arg-type]


# ============ run_with_resumption ============

def test_run_completes_in_one_iteration(monkeypatch):
    """Typical fast path: Apps Script returns status=complete on first call."""
    from src.tools import apps_script_api

    calls = []

    def fake_run(script_id, fn, params=None, dev_mode=True, account="main"):
        calls.append((fn, list(params or [])))
        return {"ok": True, "result": {
            "status": "complete",
            "value": 12345,
            "per_file_count": 10,
            "errors_count": 0,
            "errors": [],
            "_meta": {"op": "sum", "sheet": "Год факт", "cell": "B45", "duration_ms": 1500},
        }}

    monkeypatch.setattr(apps_script_api, "run_function", fake_run)

    result = chunked.run_with_resumption(
        spreadsheet_ids=[f"id{i}" for i in range(10)],
        sheet="Год факт", cell="B45", op="sum",
        script_id="FAKE_SCRIPT",
    )
    assert result["value"] == 12345
    assert result["stats"]["sum"] == 12345
    assert result["_meta"]["iterations_used"] == 1
    assert result["_meta"]["apps_script_duration_ms"] == 1500
    assert len(calls) == 1


def test_run_resumes_via_token(monkeypatch):
    """First call returns incomplete with token; second call returns complete."""
    from src.tools import apps_script_api

    calls = []

    def fake_run(script_id, fn, params=None, dev_mode=True, account="main"):
        calls.append(list(params or []))
        if len(calls) == 1:
            return {"ok": True, "result": {
                "status": "incomplete",
                "token": "resume-token-xyz",
                "processed_count": 250,
                "remaining_count": 250,
                "_meta": {"op": "sum"},
            }}
        return {"ok": True, "result": {
            "status": "complete",
            "value": 999,
            "per_file_count": 500,
            "errors_count": 0,
            "errors": [],
            "_meta": {"op": "sum", "duration_ms": 4000},
        }}

    monkeypatch.setattr(apps_script_api, "run_function", fake_run)

    result = chunked.run_with_resumption(
        spreadsheet_ids=[f"id{i}" for i in range(500)],
        sheet="S", cell="A1", op="sum",
        script_id="FAKE_SCRIPT",
    )
    assert result["value"] == 999
    assert result["_meta"]["iterations_used"] == 2
    # Second call carried the resume token as the 5th param
    assert calls[1][-1] == "resume-token-xyz"
    assert len(calls) == 2


def test_run_propagates_apps_script_error(monkeypatch):
    """status='error' from Apps Script bubbles up as RuntimeError."""
    from src.tools import apps_script_api

    def fake_run(script_id, fn, params=None, dev_mode=True, account="main"):
        return {"ok": False, "error_message": "unknown op: bogus", "error_type": "ScriptError"}

    monkeypatch.setattr(apps_script_api, "run_function", fake_run)

    with pytest.raises(RuntimeError, match="unknown op"):
        chunked.run_with_resumption(
            ["id1"], sheet="S", cell="A1", op="sum",
            script_id="FAKE",
        )


def test_run_caps_at_max_iterations(monkeypatch):
    """If Apps Script never completes, we raise after max_iterations."""
    from src.tools import apps_script_api

    def fake_run(script_id, fn, params=None, dev_mode=True, account="main"):
        return {"ok": True, "result": {
            "status": "incomplete",
            "token": "stuck-token",
            "processed_count": 1,
            "remaining_count": 999,
            "_meta": {"op": "sum"},
        }}

    monkeypatch.setattr(apps_script_api, "run_function", fake_run)

    with pytest.raises(RuntimeError, match="did not complete"):
        chunked.run_with_resumption(
            ["id1"], sheet="S", cell="A1", op="sum",
            script_id="FAKE",
            max_iterations=3,
        )


def test_run_validates_args():
    with pytest.raises(ValueError, match="non-empty"):
        chunked.run_with_resumption([], "S", "A1", "sum", script_id="FAKE")
    with pytest.raises(ValueError, match="sheet and cell"):
        chunked.run_with_resumption(["id1"], "", "A1", "sum", script_id="FAKE")
    with pytest.raises(ValueError, match="unknown op"):
        chunked.run_with_resumption(["id1"], "S", "A1", "bogus", script_id="FAKE")
    with pytest.raises(ValueError, match="max_iterations"):
        chunked.run_with_resumption(["id1"], "S", "A1", "sum", script_id="FAKE", max_iterations=0)


def test_incomplete_without_token_raises(monkeypatch):
    """Defensive: Apps Script malformed response (incomplete but no token)."""
    from src.tools import apps_script_api

    def fake_run(script_id, fn, params=None, dev_mode=True, account="main"):
        return {"ok": True, "result": {"status": "incomplete"}}  # missing token

    monkeypatch.setattr(apps_script_api, "run_function", fake_run)

    with pytest.raises(RuntimeError, match="without token"):
        chunked.run_with_resumption(["id1"], "S", "A1", "sum", script_id="FAKE")


# ============ sheets.cross_aggregate (Python wrapper) ============

def test_cross_aggregate_dry_run_returns_estimate():
    from src.tools import sheets
    result = sheets.cross_aggregate(
        spreadsheet_ids=[f"id{i}" for i in range(500)],
        sheet="S", cell="A1", op="sum",
        dry_run=True,
    )
    assert result["_meta"]["dry_run"] is True
    # With chunk_size=100 default, 500 books → 5 chunks → 5 Apps Script calls
    assert result["estimated_api_calls"] == 5
    assert result["_meta"]["n_chunks"] == 5
    assert result["estimated_quota_pressure"] == "ok"  # apps-script bucket exempt


def test_cross_aggregate_routes_to_chunked(monkeypatch):
    """sheets.cross_aggregate delegates to _apps_script_chunked.run_chunked_parallel."""
    from src.tools import sheets, _phase14_config

    monkeypatch.setattr(_phase14_config, "get_aggregator_script_id", lambda: "FAKE_SCRIPT")

    seen_args = []
    def fake_run(spreadsheet_ids, sheet, cell, op, script_id,
                 chunk_size=100, max_concurrent=5, max_iterations=5, account="main"):
        seen_args.append({"ids": spreadsheet_ids, "sheet": sheet, "cell": cell,
                          "op": op, "script_id": script_id,
                          "chunk_size": chunk_size, "max_concurrent": max_concurrent,
                          "max_iter": max_iterations})
        return {"value": 777, "_meta": {"tool": "sheets_cross_aggregate"}}

    monkeypatch.setattr("src.tools._apps_script_chunked.run_chunked_parallel", fake_run)

    result = sheets.cross_aggregate(["a", "b"], "S", "A1", op="max", max_iterations=3)
    assert result["value"] == 777
    assert seen_args[0]["script_id"] == "FAKE_SCRIPT"
    assert seen_args[0]["op"] == "max"
    assert seen_args[0]["max_iter"] == 3


# ============ run_chunked_parallel ============

def test_chunked_small_input_skips_chunking(monkeypatch):
    """N <= chunk_size goes through run_with_resumption (no chunking overhead)."""
    seen = []
    def fake_rwr(ids, sheet, cell, op, script_id, max_iterations, account="main"):
        seen.append(("rwr", len(ids)))
        return {"value": 42, "stats": {"n_ok": 3, "n_err": 0}, "errors": [],
                "_meta": {"tool": "sheets_cross_aggregate", "apps_script_duration_ms": 1000}}

    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    r = chunked.run_chunked_parallel(["a", "b", "c"], "S", "A1", "sum",
                                      script_id="FK", chunk_size=100)
    assert r["value"] == 42
    assert len(seen) == 1  # single call


def test_chunked_large_input_splits_and_merges(monkeypatch):
    """N=500 with chunk_size=100 → 5 parallel chunks merged."""
    call_count = [0]
    def fake_rwr(ids, sheet, cell, op, script_id, max_iterations, account="main"):
        call_count[0] += 1
        # Each chunk returns sum = 100 * its size
        return {
            "value": 100 * len(ids),
            "stats": {"n_ok": len(ids), "n_err": 0},
            "errors": [],
            "_meta": {"tool": "sheets_cross_aggregate", "apps_script_duration_ms": 1500},
        }

    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    ids = [f"id{i}" for i in range(500)]
    r = chunked.run_chunked_parallel(ids, "S", "A1", "sum",
                                      script_id="FK", chunk_size=100, max_concurrent=5)
    assert call_count[0] == 5
    assert r["value"] == 5 * (100 * 100)  # 5 chunks × 10000 each = 50000
    assert r["stats"]["n_ok"] == 500
    assert r["_meta"]["chunked"] is True
    assert r["_meta"]["chunks_used"] == 5


def test_chunked_op_max_takes_max_across_chunks(monkeypatch):
    """For op=max, merged value is max across chunk values."""
    chunk_values = [5, 10, 3, 12, 8]
    idx = [0]
    def fake_rwr(ids, sheet, cell, op, script_id, max_iterations, account="main"):
        v = chunk_values[idx[0]]
        idx[0] += 1
        return {"value": v, "stats": {"n_ok": len(ids), "n_err": 0}, "errors": [],
                "_meta": {"tool": "sheets_cross_aggregate"}}
    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    ids = [f"i{i}" for i in range(500)]
    r = chunked.run_chunked_parallel(ids, "S", "A1", "max",
                                      script_id="FK", chunk_size=100, max_concurrent=5)
    assert r["value"] == 12  # max of chunk values
    assert r["stats"]["max"] == 12


def test_chunked_op_avg_weighted(monkeypatch):
    """For op=avg, merged value is weighted by per-chunk n_ok."""
    # 3 chunks: 100 books avg=10, 100 books avg=20, 100 books avg=30
    # weighted = (10*100 + 20*100 + 30*100) / 300 = 20
    pairs = [(10, 100), (20, 100), (30, 100)]
    idx = [0]
    def fake_rwr(ids, sheet, cell, op, script_id, max_iterations, account="main"):
        v, n_ok = pairs[idx[0]]
        idx[0] += 1
        return {"value": v, "stats": {"n_ok": n_ok, "n_err": 0}, "errors": [],
                "_meta": {"tool": "sheets_cross_aggregate"}}
    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    ids = [f"i{i}" for i in range(300)]
    r = chunked.run_chunked_parallel(ids, "S", "A1", "avg",
                                      script_id="FK", chunk_size=100, max_concurrent=3)
    assert r["value"] == 20


def test_chunked_partial_failure_surfaced(monkeypatch):
    """If 1 of 5 chunks fails, the other 4 still merge; failure is reported."""
    call = [0]
    def fake_rwr(ids, sheet, cell, op, script_id, max_iterations, account="main"):
        call[0] += 1
        if call[0] == 3:
            raise RuntimeError("simulated network blip")
        return {"value": 100 * len(ids),
                "stats": {"n_ok": len(ids), "n_err": 0},
                "errors": [],
                "_meta": {"tool": "sheets_cross_aggregate"}}
    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    ids = [f"i{i}" for i in range(500)]
    r = chunked.run_chunked_parallel(ids, "S", "A1", "sum",
                                      script_id="FK", chunk_size=100, max_concurrent=5)
    # 4 successful chunks × 10000 each = 40000 (one chunk lost)
    assert r["value"] == 40000
    assert r["stats"]["n_ok"] == 400
    assert r["_meta"]["chunk_failures"] == 1
    assert r["_meta"]["truncated"] is True


def test_chunked_all_failures_raise(monkeypatch):
    def fake_rwr(*args, **kwargs):
        raise RuntimeError("everything is broken")
    monkeypatch.setattr(chunked, "run_with_resumption", fake_rwr)
    ids = [f"i{i}" for i in range(200)]
    with pytest.raises(RuntimeError, match="All chunks failed"):
        chunked.run_chunked_parallel(ids, "S", "A1", "sum",
                                      script_id="FK", chunk_size=100)


def test_cross_aggregate_status_routes(monkeypatch):
    from src.tools import sheets, _phase14_config

    monkeypatch.setattr(_phase14_config, "get_aggregator_script_id", lambda: "FAKE_SCRIPT")

    captured = []
    def fake_fetch(token, script_id, account="main"):
        captured.append((token, script_id))
        return {"status": "incomplete", "processed_count": 100}

    monkeypatch.setattr("src.tools._apps_script_chunked.fetch_status", fake_fetch)

    result = sheets.cross_aggregate_status("some-token")
    assert result["processed_count"] == 100
    assert captured[0] == ("some-token", "FAKE_SCRIPT")


def test_cross_aggregate_raises_on_missing_config(monkeypatch, tmp_path):
    """First call surfaces Phase14ConfigError with setup hint."""
    from src.tools import sheets, _phase14_config

    monkeypatch.setattr(_phase14_config, "CONFIG_PATH", tmp_path / "noexist.json")
    monkeypatch.delenv(_phase14_config.ENV_VAR, raising=False)

    with pytest.raises(_phase14_config.Phase14ConfigError, match="PHASE_14_SETUP"):
        sheets.cross_aggregate(["id1"], "S", "A1")


# ============ registration ============

def test_cross_aggregate_registered():
    from src.tools import registry
    names = {t["name"] for t in registry.TOOLS}
    assert "sheets_cross_aggregate" in names
    assert "sheets_cross_aggregate_status" in names


def test_cross_aggregate_schema_requires_ids_sheet_cell():
    from src.tools import registry
    spec = next(t for t in registry.TOOLS if t["name"] == "sheets_cross_aggregate")
    req = spec["schema"]["input_schema"]["required"]
    assert set(req) == {"spreadsheet_ids", "sheet", "cell"}
