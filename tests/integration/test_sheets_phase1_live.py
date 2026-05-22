"""Phase 1 live integration — sheets provenance tools against CLAUDE-TEST.

Each test creates a fresh spreadsheet under a per-test subfolder of
`CLAUDE-TEST/phase-1/`. Subfolders are NEVER auto-cleaned per project policy.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_sheets_phase1_live.py -v
"""
import pytest

pytestmark = pytest.mark.integration


def _create_test_spreadsheet(claude_test_subfolder, account, title):
    """Helper: create a spreadsheet directly inside the test subfolder."""
    from src.tools import drive, sheets

    ss = sheets.create_spreadsheet(title, account=account)
    sid = ss["spreadsheetId"]
    drive.move(sid, claude_test_subfolder, account=account)
    return sid


def _default_sheet_id_and_name(sid, account):
    """Find the first (default) sheet's id + name — Google may localize 'Sheet1'."""
    from src.tools import sheets

    meta = sheets.get_metadata(sid, account=account)
    props = meta["sheets"][0]["properties"]
    return props["sheetId"], props["title"]


def test_batch_read_multi_range(claude_test_subfolder, claude_test_account):
    """Four ranges in one call should return four populated entries."""
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-batch-read")
    _, default = _default_sheet_id_and_name(sid, claude_test_account)
    # Add 3 more sheets for cross-tab batch read
    svc = sheets._service(claude_test_account)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [
            {"addSheet": {"properties": {"title": "TabB"}}},
            {"addSheet": {"properties": {"title": "TabC"}}},
            {"addSheet": {"properties": {"title": "TabD"}}},
        ]},
    ).execute()
    # Populate each tab's A1 with a marker
    sheets.write_range(sid, f"'{default}'!A1", [["alpha"]], account=claude_test_account)
    sheets.write_range(sid, "'TabB'!A1", [["beta"]], account=claude_test_account)
    sheets.write_range(sid, "'TabC'!A1", [["gamma"]], account=claude_test_account)
    sheets.write_range(sid, "'TabD'!A1", [["delta"]], account=claude_test_account)

    result = sheets.batch_read(
        sid,
        [f"'{default}'!A1", "'TabB'!A1", "'TabC'!A1", "'TabD'!A1"],
        account=claude_test_account,
    )
    assert result["_meta"]["requested_count"] == 4
    assert result["_meta"]["returned_count"] == 4
    values = [p["values"][0][0] for p in result["per_range"]]
    assert values == ["alpha", "beta", "gamma", "delta"]


def test_formatted_vs_raw_currency(claude_test_subfolder, claude_test_account):
    """Currency-formatted cell: raw is a number, formatted is the display string."""
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-formatted")
    sheet_id, default = _default_sheet_id_and_name(sid, claude_test_account)

    # Write 3087967 then apply Russian Ruble currency format to B1.
    sheets.write_range(sid, f"'{default}'!B1", [[3087967]], account=claude_test_account)
    svc = sheets._service(claude_test_account)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 1, "endColumnIndex": 2,
                },
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "CURRENCY", "pattern": "#,##0 [$₽-419]"},
                }},
                "fields": "userEnteredFormat.numberFormat",
            },
        }]},
    ).execute()

    raw = sheets.read_range(sid, f"'{default}'!B1", account=claude_test_account)
    fmt = sheets.read_range(sid, f"'{default}'!B1", formatted=True, account=claude_test_account)

    assert raw["values"][0][0] == 3087967  # raw integer
    assert raw["_meta"]["value_mode"] == "raw"
    assert isinstance(fmt["values"][0][0], str)
    assert "₽" in fmt["values"][0][0] or "3" in fmt["values"][0][0]  # display has the symbol or at least the digit
    assert fmt["_meta"]["value_mode"] == "formatted"


def test_named_ranges_round_trip(claude_test_subfolder, claude_test_account):
    """Create a named range, list it, read it by name."""
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-named-ranges")
    _, default = _default_sheet_id_and_name(sid, claude_test_account)

    # Seed B45 with the target value
    sheets.write_range(sid, f"'{default}'!B45", [[3087967]], account=claude_test_account)

    created = sheets.create_named_range(
        sid, "ChistayaPribylGod", f"{default}!B45", account=claude_test_account,
    )
    assert created["ok"]
    assert created["named_range_id"]

    listing = sheets.list_named_ranges(sid, account=claude_test_account)
    names = [nr["name"] for nr in listing["named_ranges"]]
    assert "ChistayaPribylGod" in names

    read_back = sheets.read_named_range(sid, "ChistayaPribylGod", account=claude_test_account)
    assert read_back["values"][0][0] == 3087967
    assert read_back["_meta"]["name"] == "ChistayaPribylGod"


def test_named_range_unicode_name(claude_test_subfolder, claude_test_account):
    """A named range with Cyrillic name should round-trip cleanly.

    Note: Google Sheets named ranges are restricted to letters/digits/underscores;
    Cyrillic letters work but spaces and special chars don't. Test the
    Cyrillic-letters case.
    """
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-named-unicode")
    _, default = _default_sheet_id_and_name(sid, claude_test_account)

    sheets.write_range(sid, f"'{default}'!C7", [["unicode"]], account=claude_test_account)
    created = sheets.create_named_range(
        sid, "ПрибыльГод", f"{default}!C7", account=claude_test_account,
    )
    assert created["ok"]
    read_back = sheets.read_named_range(sid, "ПрибыльГод", account=claude_test_account)
    assert read_back["values"][0][0] == "unicode"


def test_duplicate_sheet_creates_copy_in_same_book(claude_test_subfolder, claude_test_account):
    """duplicate_sheet should create a new tab inside the same spreadsheet."""
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-duplicate")
    source_sheet_id, default = _default_sheet_id_and_name(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!A1", [["seed"]], account=claude_test_account)

    result = sheets.duplicate_sheet(sid, source_sheet_id, "Год факт 2026", account=claude_test_account)
    assert result["title"] == "Год факт 2026"
    assert isinstance(result["new_sheet_id"], int)

    # Confirm the new tab actually exists and has the seed value
    read = sheets.read_range(sid, "'Год факт 2026'!A1", account=claude_test_account)
    assert read["values"][0][0] == "seed"


def test_copy_sheet_to_different_spreadsheet(claude_test_subfolder, claude_test_account):
    """copy_sheet_to should make a copy in a DIFFERENT spreadsheet."""
    from src.tools import sheets

    src_sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-copy-src")
    dst_sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-copy-dst")
    src_sheet_id, src_default = _default_sheet_id_and_name(src_sid, claude_test_account)
    sheets.write_range(src_sid, f"'{src_default}'!A1", [["from-src"]], account=claude_test_account)

    result = sheets.copy_sheet_to(src_sid, src_sheet_id, dst_sid, account=claude_test_account)
    assert result["dest_spreadsheet_id"] == dst_sid
    assert isinstance(result["copied_sheet_id"], int)

    # Find the new tab in destination — Google prefixes with "Копия " / "Copy of "
    dst_meta = sheets.get_metadata(dst_sid, account=claude_test_account)
    new_tab_title = next(
        s["properties"]["title"] for s in dst_meta["sheets"]
        if s["properties"]["sheetId"] == result["copied_sheet_id"]
    )
    read = sheets.read_range(dst_sid, f"'{new_tab_title}'!A1", account=claude_test_account)
    assert read["values"][0][0] == "from-src"


def test_batch_read_garbage_mix(claude_test_subfolder, claude_test_account):
    """Garbage mix: valid range, empty range, and a unicode-named tab."""
    from src.tools import sheets

    sid = _create_test_spreadsheet(claude_test_subfolder, claude_test_account, "Phase1-batch-garbage")
    _, default = _default_sheet_id_and_name(sid, claude_test_account)
    svc = sheets._service(claude_test_account)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [{"addSheet": {"properties": {"title": "Юникод 2026"}}}]},
    ).execute()
    sheets.write_range(sid, f"'{default}'!A1", [["x", "y"], ["1", "2"]], account=claude_test_account)
    # 'Юникод 2026'!A1 deliberately left empty
    result = sheets.batch_read(
        sid,
        [f"'{default}'!A1:B2", "'Юникод 2026'!A1", f"'{default}'!Z99"],
        account=claude_test_account,
    )
    assert result["_meta"]["requested_count"] == 3
    # First should have 2 rows
    assert result["per_range"][0]["row_count"] == 2
    # Second and third should be empty
    assert result["per_range"][1]["empty"] is True
    assert result["per_range"][2]["empty"] is True
