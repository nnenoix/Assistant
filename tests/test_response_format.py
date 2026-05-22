"""`response_format: concise | detailed` enum on thick tools.

Anthropic engineering guide («Writing effective tools for agents») calls
out that thick tools should have a concise-by-default response format and
let the caller opt in to the full payload. Saves a lot of tokens on the
common case where the agent only needs the head of a result.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import sheets


def _make_query_response(num_rows: int):
    """Build a fake batchUpdate + values.get response that has `num_rows`
    data rows in the temp sheet."""
    fake_svc = MagicMock()
    fake_svc.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 999}}}],
    }
    rows = [["header_a", "header_b"]] + [[f"a{i}", i * 10] for i in range(num_rows)]
    fake_svc.spreadsheets().values().get().execute.return_value = {"values": rows}
    return fake_svc


def test_sheets_query_concise_returns_50_rows():
    fake = _make_query_response(120)
    with patch.object(sheets, "_service", return_value=fake), \
         patch.object(sheets, "_arg_sep", return_value=","):
        result = sheets.query("SID", "Sheet!A:B", "SELECT A, B")
    # concise: head=50 + header = 50 (since concise check is len(values) > 50)
    assert len(result["rows"]) == 50
    assert result["row_count"] == 121  # 120 data + 1 header (before trim, but trim happens on empty rows only)
    assert result["_meta"]["response_format"] == "concise"
    assert result["_meta"]["truncated"] is True
    assert "concise" in result["_meta"]["truncation_reason"]


def test_sheets_query_detailed_returns_all_rows():
    fake = _make_query_response(120)
    with patch.object(sheets, "_service", return_value=fake), \
         patch.object(sheets, "_arg_sep", return_value=","):
        result = sheets.query("SID", "Sheet!A:B", "SELECT A, B", response_format="detailed")
    assert len(result["rows"]) == 121  # all rows
    assert result["_meta"]["response_format"] == "detailed"
    assert result["_meta"]["truncated"] is False


def test_sheets_query_small_result_not_marked_truncated():
    """20 rows fits under the 50-row concise cap → no truncation flag."""
    fake = _make_query_response(20)
    with patch.object(sheets, "_service", return_value=fake), \
         patch.object(sheets, "_arg_sep", return_value=","):
        result = sheets.query("SID", "Sheet!A:B", "SELECT A, B")
    assert result["_meta"]["truncated"] is False
    assert result["_meta"]["truncation_reason"] is None


def test_sheets_query_invalid_response_format_raises():
    with pytest.raises(ValueError, match="response_format"):
        sheets.query("SID", "Sheet!A:B", "SELECT A", response_format="garbage")


def test_sheets_query_schema_advertises_enum():
    """The registry schema must surface response_format as an enum so the
    agent knows the valid values without trial-and-error."""
    from src.tools.registry import TOOLS
    spec = next(t for t in TOOLS if t["name"] == "sheets_query")
    props = spec["schema"]["input_schema"]["properties"]
    assert "response_format" in props
    assert props["response_format"]["enum"] == ["concise", "detailed"]
    assert props["response_format"]["default"] == "concise"
