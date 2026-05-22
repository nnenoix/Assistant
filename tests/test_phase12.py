"""Unit tests for Phase 12B helpers: metric_lookup, write_and_verify,
verify_claim compact form."""
from unittest.mock import MagicMock, patch

import pytest


# ---------- verify_claim compact form ----------

def test_compact_parse_sheets_cell():
    from src.tools import verify
    r = verify._parse_compact_ref("sheets:SID:Год факт!B45=3087967")
    assert r == {
        "kind": "sheets_cell",
        "spreadsheet_id": "SID",
        "cell": "Год факт!B45",
        "expected": 3087967,
    }


def test_compact_parse_named_range():
    from src.tools import verify
    r = verify._parse_compact_ref("named:SID:ChistayaPribyl=3087967")
    assert r["kind"] == "named_range"
    assert r["name"] == "ChistayaPribyl"
    assert r["expected"] == 3087967


def test_compact_parse_drive_file_with_expected_name():
    from src.tools import verify
    r = verify._parse_compact_ref("drive:F_123=ОПиУ 2026")
    assert r == {"kind": "drive_file", "file_id": "F_123", "expected_name": "ОПиУ 2026"}


def test_compact_parse_existence_check_no_expected():
    from src.tools import verify
    r = verify._parse_compact_ref("drive:F_123")
    assert r == {"kind": "drive_file", "file_id": "F_123"}


def test_compact_parse_gmail_and_calendar():
    from src.tools import verify
    g = verify._parse_compact_ref("gmail:M_123=invoice")
    assert g["kind"] == "gmail_message"
    assert g["expected_subject_contains"] == "invoice"
    c = verify._parse_compact_ref("calendar:E_42=weekly sync")
    assert c["kind"] == "calendar_event"
    assert c["expected_summary_contains"] == "weekly sync"


def test_compact_parse_float_and_string_expected():
    from src.tools import verify
    r1 = verify._parse_compact_ref("sheets:SID:A1=3.14")
    assert r1["expected"] == 3.14
    r2 = verify._parse_compact_ref("sheets:SID:A1=hello")
    assert r2["expected"] == "hello"


def test_compact_parse_unknown_kind_raises():
    from src.tools import verify
    with pytest.raises(ValueError, match="unknown kind"):
        verify._parse_compact_ref("magic:X=1")


def test_compact_parse_missing_separator_raises():
    from src.tools import verify
    with pytest.raises(ValueError, match="missing ':'"):
        verify._parse_compact_ref("sheetsSID")


def test_verify_claim_accepts_compact_string_form():
    """Mixed list of compact strings + dicts should work."""
    from src.tools import verify

    with patch("src.tools.sheets.read_range") as mock_read:
        mock_read.return_value = {
            "values": [[3087967]],
            "_meta": {"range_read": "Год факт!B45"},
        }
        result = verify.verify_claim(
            "Net profit = 3 087 967 ₽",
            ["sheets:SID:Год факт!B45=3087967"],
        )
    assert result["verdict"] == "ok"


def test_verify_claim_mixed_compact_and_dict():
    from src.tools import verify

    with patch("src.tools.sheets.read_range") as mock_read, \
         patch("src.tools.drive.get_metadata") as mock_meta:
        mock_read.return_value = {"values": [[42]], "_meta": {"range_read": "A1"}}
        mock_meta.return_value = {"id": "F1", "name": "Title"}
        result = verify.verify_claim(
            "test",
            [
                "sheets:SID:A1=42",
                {"kind": "drive_file", "file_id": "F1", "expected_name": "Title"},
            ],
        )
    assert result["verdict"] == "ok"
    assert len(result["per_ref"]) == 2


# ---------- sheets_metric_lookup ----------

def test_metric_lookup_named_range_strategy(monkeypatch):
    """When a Cyrillic-named range fuzzy-matches `metric`, use named_range strategy.

    Named ranges in Google Sheets can use Cyrillic letters but NOT spaces;
    common convention is `Чистая_прибыль` or `ЧистаяПрибыль`. Substring
    match on lowercased + underscore-stripped form.
    """
    from src.tools import sheets

    with patch.object(sheets, "list_named_ranges") as mock_list, \
         patch.object(sheets, "read_named_range") as mock_read_nr:
        mock_list.return_value = {
            "named_ranges": [
                {"name": "Чистая_прибыль_Год", "range": "'Год'!B45", "sheet": "Год"},
            ],
        }
        mock_read_nr.return_value = {
            "values": [[3087967]],
            "_meta": {"range_read": "'Год'!B45"},
        }
        result = sheets.metric_lookup("SID", "Чистая прибыль")
    assert result["value"] == 3087967
    assert result["_meta"]["strategy"] == "named_range"


def test_metric_lookup_skips_named_when_period_given(monkeypatch):
    """If period is set we skip named_range (period-specific isn't reliable)."""
    from src.tools import sheets

    with patch.object(sheets, "list_named_ranges") as mock_list, \
         patch.object(sheets, "find_in_spreadsheet") as mock_find:
        mock_list.return_value = {"named_ranges": [{"name": "ChistayaPribyl", "range": "'A'!B1", "sheet": "A"}]}
        mock_find.return_value = {"matches": [], "_meta": {}}
        result = sheets.metric_lookup("SID", "Чистая прибыль", period="Год факт")
    # named_range strategy must NOT have been invoked (read_named_range not called)
    assert result["_meta"]["strategy"] != "named_range"


def test_metric_lookup_find_with_labels_then_period(monkeypatch):
    """find_in_spreadsheet finds the metric label row; metric_lookup reads
    the row + header to find the requested period column."""
    from src.tools import sheets

    with patch.object(sheets, "list_named_ranges") as mock_list, \
         patch.object(sheets, "find_in_spreadsheet") as mock_find, \
         patch.object(sheets, "batch_read") as mock_batch:
        mock_list.return_value = {"named_ranges": []}
        mock_find.return_value = {
            "matches": [{
                "sheet": "Год факт",
                "cell": "'Год факт'!A2",
                "row": 2,
                "col": 1,
                "value": "Чистая прибыль",
                "row_label": None,
                "col_label": None,
            }],
            "_meta": {},
        }
        # batch_read returns row + header
        mock_batch.return_value = {
            "per_range": [
                {"values": [["Чистая прибыль", 100, 200, 3087967]]},  # row 2
                {"values": [["", "Янв", "Фев", "Год"]]},               # header
            ],
        }
        result = sheets.metric_lookup("SID", "Чистая прибыль", period="Год")
    assert result["value"] == 3087967
    assert result["col_label"] == "Год"
    assert result["row_label"] == "Чистая прибыль"
    assert result["_meta"]["strategy"] == "period_filter"


def test_metric_lookup_no_period_returns_last_column(monkeypatch):
    from src.tools import sheets

    with patch.object(sheets, "list_named_ranges") as mock_list, \
         patch.object(sheets, "find_in_spreadsheet") as mock_find, \
         patch.object(sheets, "batch_read") as mock_batch:
        mock_list.return_value = {"named_ranges": []}
        mock_find.return_value = {
            "matches": [{"sheet": "S", "cell": "'S'!A2", "row": 2, "col": 1, "value": "Выручка"}],
            "_meta": {},
        }
        mock_batch.return_value = {
            "per_range": [
                {"values": [["Выручка", 100, 200, 999]]},
                {"values": [["", "Янв", "Фев", "Год"]]},
            ],
        }
        result = sheets.metric_lookup("SID", "Выручка")
    # With no period → take last non-empty cell → 999
    assert result["value"] == 999
    assert result["col_label"] == "Год"


def test_metric_lookup_no_match_returns_candidates_empty():
    from src.tools import sheets

    with patch.object(sheets, "list_named_ranges") as mock_list, \
         patch.object(sheets, "find_in_spreadsheet") as mock_find:
        mock_list.return_value = {"named_ranges": []}
        mock_find.return_value = {"matches": [], "_meta": {}}
        result = sheets.metric_lookup("SID", "что-то несуществующее")
    assert result["value"] is None
    assert result["_meta"]["strategy"] is None
    assert result["_meta"]["candidates_seen"] == 0
    assert "no row" in result["_meta"]["reason"]


# ---------- sheets_write_and_verify ----------

def test_write_and_verify_ok_when_round_trip_clean(monkeypatch):
    from src.tools import sheets

    with patch.object(sheets, "snapshot_range") as mock_snap, \
         patch.object(sheets, "write_range") as mock_write, \
         patch.object(sheets, "read_range") as mock_read:
        mock_snap.return_value = {"values": [], "_meta": {}}
        mock_write.return_value = {"updatedCells": 4, "snapshot_id": "snap_1"}
        mock_read.return_value = {"values": [[1, 2], [3, 4]], "_meta": {}}
        result = sheets.write_and_verify("SID", "S!A1:B2", [[1, 2], [3, 4]])
    assert result["verdict"] == "ok"
    assert result["discrepancies"] == []
    assert result["_meta"]["discrepancy_count"] == 0


def test_write_and_verify_modified_when_sheets_evaluates_formula(monkeypatch):
    """If we write `=1+1` and Sheets evaluates to 2, that's a 'modified' verdict."""
    from src.tools import sheets

    with patch.object(sheets, "snapshot_range") as mock_snap, \
         patch.object(sheets, "write_range") as mock_write, \
         patch.object(sheets, "read_range") as mock_read:
        mock_snap.return_value = {"values": [], "_meta": {}}
        mock_write.return_value = {"updatedCells": 1, "snapshot_id": "s2"}
        mock_read.return_value = {"values": [[2]], "_meta": {}}
        result = sheets.write_and_verify("SID", "S!A1", [["=1+1"]])
    assert result["verdict"] == "modified"
    assert len(result["discrepancies"]) == 1
    d = result["discrepancies"][0]
    assert d["expected"] == "=1+1"
    assert d["actual"] == 2


def test_write_and_verify_handles_none_and_empty_as_equivalent(monkeypatch):
    """Writing "" and reading back None should NOT be flagged as discrepancy."""
    from src.tools import sheets

    with patch.object(sheets, "snapshot_range") as mock_snap, \
         patch.object(sheets, "write_range") as mock_write, \
         patch.object(sheets, "read_range") as mock_read:
        mock_snap.return_value = {"values": [], "_meta": {}}
        mock_write.return_value = {"updatedCells": 1, "snapshot_id": "s3"}
        mock_read.return_value = {"values": [[None, 5]], "_meta": {}}
        result = sheets.write_and_verify("SID", "S!A1:B1", [["", 5]])
    assert result["verdict"] == "ok"
