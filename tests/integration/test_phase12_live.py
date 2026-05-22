"""Phase 12 live integration — error taxonomy, metric_lookup, write_and_verify,
compact verify_claim. Hits real Drive under CLAUDE-TEST/phase-12/.
"""
import pytest

pytestmark = pytest.mark.integration


def _create_book(claude_test_subfolder, account, title):
    from src.tools import drive, sheets
    ss = sheets.create_spreadsheet(title, account=account)
    sid = ss["spreadsheetId"]
    drive.move(sid, claude_test_subfolder, account=account)
    return sid


def _default(sid, account):
    from src.tools import sheets
    meta = sheets.get_metadata(sid, account=account)
    p = meta["sheets"][0]["properties"]
    return p["sheetId"], p["title"]


# ---------- metric_lookup ----------

def test_metric_lookup_named_range(claude_test_subfolder, claude_test_account):
    """Create a named range with Cyrillic name, look it up by fuzzy metric."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-metric-named")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[3087967]], account=claude_test_account)
    sheets.create_named_range(sid, "Чистая_прибыль_Год", f"{default}!B45", account=claude_test_account)

    result = sheets.metric_lookup(sid, "Чистая прибыль", account=claude_test_account)
    assert result["value"] == 3087967
    assert result["_meta"]["strategy"] == "named_range"


def test_metric_lookup_find_with_labels_then_period(claude_test_subfolder, claude_test_account):
    """Build a typical financial table (header row = periods, col A = metrics),
    look up «Чистая прибыль / Год»."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-metric-table")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:E3", [
        ["", "Янв", "Фев", "Мар", "Год"],
        ["Выручка", 100, 200, 300, 30_000_000],
        ["Чистая прибыль", 10, 20, 30, 3_087_967],
    ], account=claude_test_account)

    result = sheets.metric_lookup(sid, "Чистая прибыль", period="Год", account=claude_test_account)
    assert result["value"] == 3_087_967
    assert result["row_label"] == "Чистая прибыль"
    assert result["col_label"] == "Год"
    assert result["_meta"]["strategy"] == "period_filter"


def test_metric_lookup_no_match_returns_none(claude_test_subfolder, claude_test_account):
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-metric-nomatch")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:B2", [
        ["Период", "Сумма"],
        ["Q1", 100],
    ], account=claude_test_account)

    result = sheets.metric_lookup(sid, "EBITDA", account=claude_test_account)
    assert result["value"] is None
    assert result["_meta"]["strategy"] is None


# ---------- write_and_verify ----------

def test_write_and_verify_clean_match(claude_test_subfolder, claude_test_account):
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-wav-ok")
    _, default = _default(sid, claude_test_account)

    result = sheets.write_and_verify(
        sid, f"'{default}'!A1:B2",
        [["x", "y"], [1, 2]],
        account=claude_test_account,
    )
    assert result["verdict"] == "ok"
    assert result["discrepancies"] == []


def test_write_and_verify_formula_evaluated(claude_test_subfolder, claude_test_account):
    """Writing =1+1 → Sheets evaluates to 2 → verdict='modified' with discrepancy."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-wav-formula")
    _, default = _default(sid, claude_test_account)

    result = sheets.write_and_verify(
        sid, f"'{default}'!A1",
        [["=1+1"]],
        account=claude_test_account,
    )
    assert result["verdict"] == "modified"
    assert len(result["discrepancies"]) == 1
    assert result["discrepancies"][0]["actual"] == 2


# ---------- error taxonomy via real 404 ----------

def test_error_taxonomy_not_found_on_bad_spreadsheet(claude_test_account):
    """A missing spreadsheet should classify as not_found (404), not 'unknown'."""
    from src.tools import sheets
    from src.tools.registry import _classify_exception

    try:
        sheets.read_range("definitely_not_a_real_spreadsheet_id_12345", "A1", account=claude_test_account)
        pytest.fail("expected HttpError")
    except Exception as e:
        kind, status = _classify_exception(e)
        # Google returns 404 for missing spreadsheets
        assert kind == "not_found", f"expected not_found, got {kind} (status={status})"


# ---------- verify_claim compact form ----------

def test_verify_claim_compact_form_sheets_cell(claude_test_subfolder, claude_test_account):
    from src.tools import sheets, verify

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-verify-compact")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[3087967]], account=claude_test_account)

    result = verify.verify_claim(
        "Чистая прибыль = 3 087 967",
        [f"sheets:{sid}:{default}!B45=3087967"],
    )
    assert result["verdict"] == "ok"


def test_verify_claim_compact_form_mismatch(claude_test_subfolder, claude_test_account):
    from src.tools import sheets, verify

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase12-verify-mismatch")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[42]], account=claude_test_account)

    result = verify.verify_claim(
        "Should be 999",
        [f"sheets:{sid}:{default}!B45=999"],
    )
    assert result["verdict"] == "mismatch"
    assert len(result["discrepancies"]) == 1
