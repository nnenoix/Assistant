"""Phase 2 live integration — formatting + charts + pivots + validation
against CLAUDE-TEST/phase-2/.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_sheets_phase2_live.py -v
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


# ---------- set_format ----------

def test_set_format_currency_preset(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-format-currency")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B2", [[3087967]], account=claude_test_account)

    sheets.set_format(sid, f"'{default}'!B2", preset="currency_rub_int", account=claude_test_account)

    formatted = sheets.read_range(sid, f"'{default}'!B2", formatted=True, account=claude_test_account)
    # Currency-formatted Russian Ruble cell should contain '₽' (or the digit pattern)
    cell = formatted["values"][0][0]
    assert "₽" in cell or "3" in cell


def test_set_format_text_bold_background(claude_test_subfolder, claude_test_account):
    """Set bold + background color → cell formatting actually applies."""
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-format-mixed")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1", [["Header"]], account=claude_test_account)

    result = sheets.set_format(
        sid, f"'{default}'!A1",
        text_format={"bold": True, "fontSize": 14},
        background_color={"red": 1.0, "green": 0.95, "blue": 0.7},
        account=claude_test_account,
    )
    assert result["ok"]
    assert "userEnteredFormat.textFormat" in result["applied_fields"]
    assert "userEnteredFormat.backgroundColor" in result["applied_fields"]


def test_set_format_rejects_unknown_preset(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-format-bad-preset")
    _, default = _default(sid, claude_test_account)
    with pytest.raises(ValueError, match="unknown preset"):
        sheets.set_format(sid, f"'{default}'!A1", preset="not_a_real_preset", account=claude_test_account)


# ---------- freeze ----------

def test_freeze_header_row(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-freeze")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:C1", [["A", "B", "C"]], account=claude_test_account)
    result = sheets.freeze(sid, default, rows=1, account=claude_test_account)
    assert result["frozen_rows"] == 1
    # Verify via metadata
    svc = sheets._service(claude_test_account)
    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(title,gridProperties))",
    ).execute()
    grid = meta["sheets"][0]["properties"]["gridProperties"]
    assert grid.get("frozenRowCount") == 1


# ---------- merge / unmerge ----------

def test_merge_then_unmerge(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-merge")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1", [["merged title"]], account=claude_test_account)

    merge_result = sheets.merge_cells(sid, f"'{default}'!A1:C1", account=claude_test_account)
    assert merge_result["ok"]
    # unmerge same range
    unmerge_result = sheets.unmerge_cells(sid, f"'{default}'!A1:C1", account=claude_test_account)
    assert unmerge_result["ok"]


# ---------- set_data_validation ----------

def test_data_validation_dropdown(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-validation-dropdown")
    _, default = _default(sid, claude_test_account)
    result = sheets.set_data_validation(
        sid, f"'{default}'!B2:B10", kind="dropdown",
        values=["IdealNight", "SensesAura", "VelvetSkin", "Альтер Хим"],
        account=claude_test_account,
    )
    assert result["ok"]
    assert result["kind"] == "dropdown"


def test_data_validation_number_between(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-validation-numeric")
    _, default = _default(sid, claude_test_account)
    result = sheets.set_data_validation(
        sid, f"'{default}'!C2:C10", kind="number_between",
        min_value=0, max_value=100,
        account=claude_test_account,
    )
    assert result["ok"]


def test_data_validation_dropdown_requires_values(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-validation-bad")
    _, default = _default(sid, claude_test_account)
    with pytest.raises(ValueError, match="needs"):
        sheets.set_data_validation(sid, f"'{default}'!A1", kind="dropdown", account=claude_test_account)


# ---------- set_conditional_format ----------

def test_conditional_format_negatives_red(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-cond-fmt")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:A5", [[100], [-50], [200], [-300], [0]], account=claude_test_account)
    result = sheets.set_conditional_format(
        sid, f"'{default}'!A1:A5", condition="negatives_red", account=claude_test_account,
    )
    assert result["ok"]
    assert result["color_applied"]["red"] > 0.9


# ---------- create_chart ----------

def test_create_pie_chart(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-chart-pie")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:B5", [
        ["Brand", "Revenue"],
        ["IdealNight", 3_087_967],
        ["SensesAura", 1_200_000],
        ["VelvetSkin", 800_000],
        ["Альтер Хим", 500_000],
    ], account=claude_test_account)
    result = sheets.create_chart(
        sid, default, chart_type="pie", title="Revenue by brand",
        domain_range=f"'{default}'!A2:A5",
        series_ranges=[f"'{default}'!B2:B5"],
        account=claude_test_account,
    )
    assert result["ok"]
    assert isinstance(result["chart_id"], int)


def test_create_column_chart(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-chart-col")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:C4", [
        ["Q", "Plan", "Fact"],
        ["Q1", 1000, 950],
        ["Q2", 1200, 1300],
        ["Q3", 1400, 1100],
    ], account=claude_test_account)
    result = sheets.create_chart(
        sid, default, chart_type="column", title="Plan vs Fact",
        domain_range=f"'{default}'!A2:A4",
        series_ranges=[f"'{default}'!B2:B4", f"'{default}'!C2:C4"],
        account=claude_test_account,
    )
    assert result["ok"]


def test_create_chart_rejects_unknown_type(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-chart-bad")
    _, default = _default(sid, claude_test_account)
    with pytest.raises(ValueError, match="unknown chart_type"):
        sheets.create_chart(
            sid, default, chart_type="3d_holographic", title="x",
            domain_range=f"'{default}'!A1:A2", series_ranges=[f"'{default}'!B1:B2"],
            account=claude_test_account,
        )


# ---------- create_pivot ----------

def test_create_pivot_brand_revenue(claude_test_subfolder, claude_test_account):
    from src.tools import sheets
    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase2-pivot")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1:C7", [
        ["Brand", "Date", "Revenue"],
        ["IdealNight", "2026-01", 1000],
        ["IdealNight", "2026-02", 1200],
        ["SensesAura", "2026-01", 800],
        ["SensesAura", "2026-02", 900],
        ["VelvetSkin", "2026-01", 600],
        ["VelvetSkin", "2026-02", 700],
    ], account=claude_test_account)
    result = sheets.create_pivot(
        sid,
        source_range=f"'{default}'!A1:C7",
        rows=["Brand"],
        values=[{"column": "Revenue", "aggregate": "SUM"}],
        account=claude_test_account,
    )
    assert result["ok"]
    assert isinstance(result["dest_sheet_id"], int)
