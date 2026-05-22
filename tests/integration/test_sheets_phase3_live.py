"""Phase 3 live integration — protected ranges + cell notes against
CLAUDE-TEST/phase-3/.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_sheets_phase3_live.py -v
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


# ---------- protected ranges ----------

def test_protected_range_add_list_remove_round_trip(claude_test_subfolder, claude_test_account):
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-protected")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[3087967]], account=claude_test_account)

    # Add
    added = sheets.add_protected_range(
        sid, f"'{default}'!B45",
        description="чистая прибыль — не трогать",
        warning_only=False,
        account=claude_test_account,
    )
    pr_id = added["protected_range_id"]
    assert isinstance(pr_id, int)

    # List
    listing = sheets.list_protected_ranges(sid, account=claude_test_account)
    assert listing["_meta"]["count"] == 1
    pr = listing["protected_ranges"][0]
    assert pr["description"] == "чистая прибыль — не трогать"
    assert pr["warning_only"] is False
    assert "B45" in pr["range"]

    # Remove
    sheets.remove_protected_range(sid, pr_id, account=claude_test_account)
    after = sheets.list_protected_ranges(sid, account=claude_test_account)
    assert after["_meta"]["count"] == 0
    assert after["_meta"]["empty_reason"] == "no_protected_ranges"


def test_protected_range_warning_only(claude_test_subfolder, claude_test_account):
    """warning_only=True should be recorded as such on list_protected_ranges."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-warn-only")
    _, default = _default(sid, claude_test_account)
    sheets.add_protected_range(
        sid, f"'{default}'!A1:C3",
        description="soft lock — confirm before editing",
        warning_only=True,
        account=claude_test_account,
    )
    listing = sheets.list_protected_ranges(sid, account=claude_test_account)
    assert listing["protected_ranges"][0]["warning_only"] is True


# ---------- cell notes ----------

def test_set_and_get_cell_note_single_cell(claude_test_subfolder, claude_test_account):
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-cell-note")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[3087967]], account=claude_test_account)

    set_result = sheets.set_cell_note(
        sid, f"'{default}'!B45",
        "источник: ОПиУ 2026 Q4; проверить с Олей",
        account=claude_test_account,
    )
    assert set_result["ok"]
    assert set_result["note_length"] > 10

    got = sheets.get_cell_notes(sid, f"'{default}'!B45", account=claude_test_account)
    assert got["_meta"]["non_empty_count"] == 1
    assert got["notes"][0][0].startswith("источник:")


def test_cell_notes_clear_via_empty_string(claude_test_subfolder, claude_test_account):
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-note-clear")
    _, default = _default(sid, claude_test_account)
    sheets.set_cell_note(sid, f"'{default}'!A1", "first", account=claude_test_account)
    sheets.set_cell_note(sid, f"'{default}'!A1", "", account=claude_test_account)
    got = sheets.get_cell_notes(sid, f"'{default}'!A1", account=claude_test_account)
    # Empty string note → API treats as no note
    assert got["_meta"]["empty_reason"] == "no_notes"


def test_get_cell_notes_2d_grid(claude_test_subfolder, claude_test_account):
    """A1:C2 range with notes scattered across cells returns a correctly shaped grid."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-notes-grid")
    _, default = _default(sid, claude_test_account)

    # Set notes on A1 and C2 only
    sheets.set_cell_note(sid, f"'{default}'!A1", "tl", account=claude_test_account)
    sheets.set_cell_note(sid, f"'{default}'!C2", "br", account=claude_test_account)

    got = sheets.get_cell_notes(sid, f"'{default}'!A1:C2", account=claude_test_account)
    assert got["_meta"]["non_empty_count"] == 2
    # The grid should be 2 rows; first row contains note at index 0, second at index 2
    flat = [n for row in got["notes"] for n in row]
    assert "tl" in flat
    assert "br" in flat


def test_protected_range_and_note_coexist(claude_test_subfolder, claude_test_account):
    """A protected range AND a note on the same cell should both stick."""
    from src.tools import sheets

    sid = _create_book(claude_test_subfolder, claude_test_account, "Phase3-prot-and-note")
    _, default = _default(sid, claude_test_account)
    sheets.write_range(sid, f"'{default}'!B45", [[42]], account=claude_test_account)

    sheets.set_cell_note(
        sid, f"'{default}'!B45",
        "защищено — не редактировать руками",
        account=claude_test_account,
    )
    sheets.add_protected_range(
        sid, f"'{default}'!B45",
        description="freeze",
        account=claude_test_account,
    )

    notes = sheets.get_cell_notes(sid, f"'{default}'!B45", account=claude_test_account)
    prots = sheets.list_protected_ranges(sid, account=claude_test_account)
    assert notes["_meta"]["non_empty_count"] == 1
    assert prots["_meta"]["count"] == 1
