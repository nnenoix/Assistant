"""Unit tests for Phase 11 helpers: sheets_run_formula, sheets_period_detect,
verify_claim, self_run_tests, self_list_tools.
"""
from unittest.mock import MagicMock, patch

import pytest


# ============ sheets_run_formula ============

def test_run_formula_returns_scalar(monkeypatch):
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99}}}],
    }
    svc.spreadsheets().values().update().execute.return_value = {}
    svc.spreadsheets().values().get().execute.return_value = {
        "values": [[89.42]],
    }
    result = sheets.run_formula("SID", '=GOOGLEFINANCE("CURRENCY:USDRUB")')
    assert result["result"] == 89.42
    assert result["_meta"]["shape"] == "scalar"
    assert result["_meta"]["is_error"] is False


def test_run_formula_returns_range(monkeypatch):
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99}}}],
    }
    svc.spreadsheets().values().update().execute.return_value = {}
    svc.spreadsheets().values().get().execute.return_value = {
        "values": [[1, 2], [3, 4]],
    }
    result = sheets.run_formula("SID", "=A1:B2")
    assert result["_meta"]["shape"] == "range"
    assert result["_meta"]["rows"] == 2


def test_run_formula_rejects_non_formula():
    from src.tools import sheets
    with pytest.raises(ValueError, match="must start with"):
        sheets.run_formula("SID", "not a formula")


def test_run_formula_detects_error_scalar(monkeypatch):
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99}}}],
    }
    svc.spreadsheets().values().update().execute.return_value = {}
    svc.spreadsheets().values().get().execute.return_value = {
        "values": [["#REF!"]],
    }
    result = sheets.run_formula("SID", "=A1+B2")
    assert result["_meta"]["is_error"] is True


# ============ sheets_period_detect ============

def test_period_detect_classifies_kinds(monkeypatch):
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().values().get().execute.return_value = {
        "values": [["", "Янв", "Фев", "Q1", "Год факт", "Год план", "2026", "Описание"]],
    }
    result = sheets.period_detect("SID", "Sheet1")
    kinds = {p["label"]: p["kind"] for p in result["periods"]}
    assert kinds["Янв"] == "month"
    assert kinds["Фев"] == "month"
    assert kinds["Q1"] == "quarter"
    assert kinds["Год факт"] == "plan_fact"
    assert kinds["Год план"] == "plan_fact"
    assert kinds["2026"] == "year"
    assert kinds["Описание"] == "other"


def test_period_detect_compound_month_year(monkeypatch):
    """'Янв 2026' should classify as month."""
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().values().get().execute.return_value = {
        "values": [["Бренд", "Янв 2026", "January 2026", "Q2 2026"]],
    }
    result = sheets.period_detect("SID", "Sheet1")
    kinds = {p["label"]: p["kind"] for p in result["periods"]}
    assert kinds["Янв 2026"] == "month"
    assert kinds["January 2026"] == "month"


def test_period_detect_empty_header(monkeypatch):
    from src.tools import sheets

    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda account="main": svc)
    svc.spreadsheets().values().get().execute.return_value = {"values": []}
    result = sheets.period_detect("SID", "Sheet1")
    assert result["_meta"]["empty_reason"] == "no_headers"


# ============ verify_claim ============

def test_verify_claim_sheets_cell_match():
    from src.tools import verify
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {
            "values": [[3087967]],
            "_meta": {"range_read": "Год факт!B45"},
        }
        result = verify.verify_claim(
            "Чистая прибыль = 3 087 967 ₽",
            [{"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "Год факт!B45", "expected": 3087967}],
        )
    assert result["verdict"] == "ok"
    assert not result["discrepancies"]


def test_verify_claim_sheets_cell_mismatch():
    from src.tools import verify
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {
            "values": [[5_000_000]],
            "_meta": {"range_read": "Год факт!B45"},
        }
        result = verify.verify_claim(
            "Чистая прибыль = 3 087 967 ₽",
            [{"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "Год факт!B45", "expected": 3087967}],
        )
    assert result["verdict"] == "mismatch"
    assert len(result["discrepancies"]) == 1
    assert result["discrepancies"][0]["actual"] == 5_000_000


def test_verify_claim_coerces_str_and_num():
    """3087967 (int) == '3087967' (str) — should match."""
    from src.tools import verify
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {"values": [["3087967"]], "_meta": {}}
        result = verify.verify_claim(
            "x",
            [{"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "B45", "expected": 3087967}],
        )
    assert result["verdict"] == "ok"


def test_verify_claim_named_range():
    from src.tools import verify
    with patch("src.tools.sheets.read_named_range") as mock_read:
        mock_read.return_value = {"values": [[42]], "_meta": {"range_read": "Sheet1!A1"}}
        result = verify.verify_claim(
            "Answer = 42",
            [{"kind": "named_range", "spreadsheet_id": "SID", "name": "Answer", "expected": 42}],
        )
    assert result["verdict"] == "ok"


def test_verify_claim_drive_file_name_mismatch():
    from src.tools import verify
    with patch("src.tools.drive.get_metadata") as mock_meta:
        mock_meta.return_value = {"id": "F1", "name": "Wrong title"}
        result = verify.verify_claim(
            "Found IdealNight ОПиУ 2026",
            [{"kind": "drive_file", "file_id": "F1", "expected_name": "IdealNight ОПиУ 2026"}],
        )
    assert result["verdict"] == "mismatch"


def test_verify_claim_unknown_kind_skipped():
    from src.tools import verify
    result = verify.verify_claim(
        "x",
        [{"kind": "telepathy", "blob": "..."}],
    )
    assert result["verdict"] == "ok"  # no actual mismatches; only a skipped
    assert result["per_ref"][0]["status"] == "skipped"


def test_verify_claim_error_propagates():
    from src.tools import verify
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.side_effect = RuntimeError("permission denied")
        result = verify.verify_claim(
            "x",
            [{"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A1", "expected": 1}],
        )
    assert result["verdict"] == "error"


# ============ Phase 14D: parallel verify_claim ============

def test_verify_claim_parallel_preserves_order():
    """Even with mixed-speed mocks, per_ref order must match input order."""
    import time as _time
    from src.tools import verify

    call_log = []

    def slow_read(spreadsheet_id, cell, formatted=False, account="main"):
        # First ref artificially slowest; per_ref must still appear at index 0.
        delay = {"A1": 0.05, "A2": 0.0, "A3": 0.01}.get(cell, 0.0)
        _time.sleep(delay)
        call_log.append(cell)
        return {"values": [[cell]], "_meta": {"range_read": cell}}

    refs = [
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A1", "expected": "A1"},
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A2", "expected": "A2"},
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A3", "expected": "A3"},
    ]
    with patch("src.tools.sheets.read_range", side_effect=slow_read):
        result = verify.verify_claim("x", refs)

    assert result["verdict"] == "ok"
    # Order preserved despite A1 being slowest
    assert [r["cell"] for r in result["per_ref"]] == ["A1", "A2", "A3"]
    assert result["_meta"]["parallel"] is True
    assert result["_meta"]["ref_count"] == 3


def test_verify_claim_parallel_runs_concurrently():
    """Total wall-clock should be ~max(delay), not sum(delays)."""
    import time as _time
    from src.tools import verify

    def slow_read(spreadsheet_id, cell, formatted=False, account="main"):
        _time.sleep(0.1)  # each ref blocks 100ms
        return {"values": [[1]], "_meta": {"range_read": cell}}

    refs = [
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": f"A{i}", "expected": 1}
        for i in range(10)
    ]
    with patch("src.tools.sheets.read_range", side_effect=slow_read):
        t0 = _time.perf_counter()
        result = verify.verify_claim("x", refs)
        elapsed = _time.perf_counter() - t0

    # Serial would be ~1.0s. Parallel with 10 workers should be ~0.1s.
    # Allow generous buffer for CI/Windows scheduler jitter.
    assert elapsed < 0.6, f"expected <0.6s wall-clock, got {elapsed:.2f}s"
    assert result["verdict"] == "ok"
    assert result["_meta"]["ref_count"] == 10


def test_verify_claim_one_error_does_not_kill_others():
    """Failure of one ref must not prevent others from completing."""
    from src.tools import verify

    def flaky_read(spreadsheet_id, cell, formatted=False, account="main"):
        if cell == "BAD":
            raise RuntimeError("simulated 500")
        return {"values": [[42]], "_meta": {"range_read": cell}}

    refs = [
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A1", "expected": 42},
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "BAD", "expected": 42},
        {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A3", "expected": 42},
    ]
    with patch("src.tools.sheets.read_range", side_effect=flaky_read):
        result = verify.verify_claim("x", refs)

    statuses = [r["status"] for r in result["per_ref"]]
    assert statuses == ["ok", "error", "ok"]
    assert result["verdict"] == "error"  # at least one error and no mismatches
    assert "simulated 500" in result["per_ref"][1]["reason"]


def test_verify_claim_max_workers_clamped():
    """max_workers clamped to refs count and to MAX_ALLOWED ceiling."""
    from src.tools import verify

    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {"values": [[1]], "_meta": {}}
        # 2 refs, request 100 workers — should clamp to ref count
        result = verify.verify_claim(
            "x",
            [
                {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A1", "expected": 1},
                {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A2", "expected": 1},
            ],
            max_workers=100,
        )
    assert result["_meta"]["max_workers"] == 2


def test_verify_claim_empty_refs_returns_ok():
    """Edge case: empty source_refs list — no work, verdict=ok."""
    from src.tools import verify
    result = verify.verify_claim("x", [])
    assert result["verdict"] == "ok"
    assert result["per_ref"] == []
    assert result["_meta"]["ref_count"] == 0


def test_verify_claim_parse_error_does_not_skip_other_refs():
    """A malformed compact string must not prevent good refs from being read."""
    from src.tools import verify
    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {"values": [[1]], "_meta": {"range_read": "A1"}}
        result = verify.verify_claim(
            "x",
            [
                "totally-not-a-ref",  # parse error
                {"kind": "sheets_cell", "spreadsheet_id": "SID", "cell": "A1", "expected": 1},
            ],
        )
    # Error in slot 0, ok in slot 1
    assert result["per_ref"][0]["status"] == "error"
    assert result["per_ref"][1]["status"] == "ok"
    assert result["verdict"] == "error"


# ============ self_run_tests ============

def test_self_run_tests_parses_pytest_summary():
    from src.tools import self_heal

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = (
        "tests/test_foo.py ...\n"
        "tests/test_bar.py ..\n"
        "5 passed in 0.42s\n"
    )
    fake_proc.stderr = ""
    with patch("subprocess.run", return_value=fake_proc):
        result = self_heal.self_run_tests("tests/test_foo.py")
    assert result["ok"]
    assert result["passed"] == 5
    assert result["failed"] == 0


def test_self_run_tests_detects_failures():
    from src.tools import self_heal

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = "10 passed, 2 failed, 1 skipped in 1.0s\n"
    fake_proc.stderr = ""
    with patch("subprocess.run", return_value=fake_proc):
        result = self_heal.self_run_tests()
    assert result["ok"] is False
    assert result["passed"] == 10
    assert result["failed"] == 2
    assert result["skipped"] == 1


# ============ self_list_tools ============

def test_self_list_tools_returns_registered_set():
    from src.tools import self_heal, registry
    result = self_heal.self_list_tools()
    assert result["_meta"]["count"] == len(registry.TOOLS)
    # All tools have name + policy_op + description
    for t in result["tools"]:
        assert "name" in t
        assert "policy_op" in t
        assert "description" in t


def test_self_list_tools_detects_account_param():
    from src.tools import self_heal
    result = self_heal.self_list_tools()
    # sheets_read_range has account param; verify_claim does not
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["sheets_read_range"]["has_account_param"] is True
    assert by_name["verify_claim"]["has_account_param"] is False
