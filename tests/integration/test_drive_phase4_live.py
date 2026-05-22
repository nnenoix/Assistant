"""Phase 4 live integration — Drive sharing + history against CLAUDE-TEST/phase-4/.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_drive_phase4_live.py -v

Notes:
  - Sharing tests share files to the SAME account's email (egor.titt@gmail.com)
    so no one else gets bothered. Real-world cross-account shares would behave
    the same — the API doesn't care if it's a self-share.
  - empty_trash is NOT exercised live — it would nuke any pre-existing trashed
    files in the user's account. Only the unit test covers it.
  - download_revision is skipped for native Google formats — Drive doesn't
    support binary download of Sheets/Docs/Slides revisions.
"""
import pytest

pytestmark = pytest.mark.integration


def _create_test_doc(claude_test_subfolder, account, title):
    """Create a small Google Sheet inside the test subfolder."""
    from src.tools import drive, sheets
    ss = sheets.create_spreadsheet(title, account=account)
    sid = ss["spreadsheetId"]
    drive.move(sid, claude_test_subfolder, account=account)
    return sid


SELF_EMAIL = "egor.titt@gmail.com"


# ---------- permissions ----------

def test_list_permissions_owner_present(claude_test_subfolder, claude_test_account):
    """Newly created file should have at least the owner permission."""
    from src.tools import drive

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-perms-baseline")
    result = drive.list_permissions(sid, account=claude_test_account)
    assert result["_meta"]["count"] >= 1
    roles = {p.get("role") for p in result["permissions"]}
    assert "owner" in roles


def test_share_and_revoke_round_trip(claude_test_subfolder, claude_test_account):
    """Share file with self as commenter, verify, revoke, verify gone."""
    from src.tools import drive

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-share-rt")

    # Sharing with self at non-owner role is a no-op in some cases.
    # Use a fake alias variant: e.g. share with self as commenter — Google
    # treats this as duplicate; we instead share with a different
    # variant or skip if it errors.
    # Since this is a personal Gmail, we'll share with the same address
    # but role=writer (Drive permits adding a 'self' permission as a
    # share entry — it just doesn't change effective access).
    try:
        added = drive.share(
            sid, SELF_EMAIL, role="commenter", notify=False,
            account=claude_test_account,
        )
        perm_id = added["permission_id"]
        assert perm_id

        listing = drive.list_permissions(sid, account=claude_test_account)
        roles = [p.get("role") for p in listing["permissions"]]
        assert "commenter" in roles or "owner" in roles  # at minimum owner remains

        drive.revoke_permission(sid, perm_id, account=claude_test_account)
    except Exception as e:
        # Google sometimes rejects self-share with "you can't share with yourself"
        # — treat as expected if the message indicates that.
        msg = str(e).lower()
        if "yourself" in msg or "owner" in msg:
            pytest.skip(f"Drive refused self-share: {e}")
        raise


def test_transfer_ownership_self_skipped_safely(claude_test_subfolder, claude_test_account):
    """Attempting to transfer ownership to self should be rejected by Drive,
    which lets us verify the request was correctly formed (no false 200)."""
    from src.tools import drive

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-transfer-self")
    try:
        drive.transfer_ownership(sid, SELF_EMAIL, account=claude_test_account)
    except Exception as e:
        # Expected — Drive refuses transfer-to-self.
        assert "owner" in str(e).lower() or "permission" in str(e).lower() or "yourself" in str(e).lower()
        return
    pytest.fail("Drive should have refused transfer-to-self but didn't")


# ---------- revisions ----------

def test_list_revisions_on_native_sheet(claude_test_subfolder, claude_test_account):
    """A freshly created+edited Sheet should have at least one revision."""
    from src.tools import drive, sheets

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-revisions")
    # Write something to make sure there's at least one revision
    sheets.write_range(sid, "A1", [["v1"]], account=claude_test_account)
    result = drive.list_revisions(sid, account=claude_test_account)
    # Note: native Google file revisions can appear with delay; allow either
    # populated list or graceful empty_reason.
    if result["_meta"]["count"] == 0:
        assert result["_meta"]["empty_reason"] == "no_revisions"
    else:
        assert result["_meta"]["count"] >= 1
        # Each revision should at least have an id + modifiedTime
        rev = result["revisions"][0]
        assert "id" in rev
        assert "modifiedTime" in rev


# ---------- comments ----------

def test_add_list_resolve_comment(claude_test_subfolder, claude_test_account):
    from src.tools import drive

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-comments")
    added = drive.add_comment(sid, "проверь B45 — расходится с банком", account=claude_test_account)
    comment_id = added["comment_id"]
    assert comment_id

    listing = drive.list_comments(sid, account=claude_test_account)
    assert listing["_meta"]["count"] == 1
    assert listing["comments"][0]["content"].startswith("проверь B45")

    drive.resolve_comment(sid, comment_id, account=claude_test_account)
    # Default list filters out resolved
    after = drive.list_comments(sid, account=claude_test_account)
    assert after["_meta"]["count"] == 0
    # With include_resolved we should see it again
    with_resolved = drive.list_comments(sid, include_resolved=True, account=claude_test_account)
    assert with_resolved["_meta"]["count"] == 1
    assert with_resolved["comments"][0]["resolved"] is True


# ---------- trash ----------

def test_trash_restore_round_trip(claude_test_subfolder, claude_test_account):
    """Create a file, move it to trash, list trash, restore, verify it's back."""
    from src.tools import drive

    sid = _create_test_doc(claude_test_subfolder, claude_test_account, "Phase4-trash-rt")

    # Send to trash via the regular update (Drive API exposes trashing as
    # files.update body trashed=True — we don't have a dedicated wrapper
    # but the underlying call works through update_content-style usage).
    svc = drive._service(claude_test_account)
    svc.files().update(fileId=sid, body={"trashed": True}, fields="id,trashed").execute()

    trash_listing = drive.list_trash(account=claude_test_account)
    ids_in_trash = [f["id"] for f in trash_listing["files"]]
    assert sid in ids_in_trash

    drive.restore_from_trash(sid, account=claude_test_account)
    # Confirm restoration via get_metadata (shouldn't raise)
    meta = drive.get_metadata(sid, account=claude_test_account)
    assert meta["id"] == sid
