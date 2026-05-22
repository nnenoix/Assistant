"""Phase 0 smoke — verifies the integration scaffolding actually works.

These tests are live: they hit Drive API against egor.titt@gmail.com /
CLAUDE-TEST. Run with `LIVE_GOOGLE_TESTS=1 pytest tests/integration/test_phase0_foundation_live.py`.

What they prove:
  1. The CLAUDE-TEST root folder exists and is reachable.
  2. The OAuth token covers the new scopes (Docs, Slides, Forms, People).
  3. The `claude_test_subfolder` fixture creates per-test subfolders correctly.
"""
import pytest

pytestmark = pytest.mark.integration


def test_root_folder_reachable(claude_test_root_id, claude_test_account):
    """CLAUDE-TEST root must exist and be readable."""
    from src.tools import drive

    meta = drive.get_metadata(claude_test_root_id, account=claude_test_account)
    assert meta["id"] == claude_test_root_id
    assert meta["name"] == "CLAUDE-TEST"
    assert meta["mimeType"] == "application/vnd.google-apps.folder"


def test_token_has_all_new_scopes(claude_test_account):
    """OAuth token must include every new scope added in Phase 0."""
    from src import auth

    info = auth.describe_account(claude_test_account)
    granted = set(info.get("scopes", []))

    required = {
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/forms.body",
        "https://www.googleapis.com/auth/forms.responses.readonly",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/contacts",
    }
    missing = required - granted
    assert not missing, (
        f"OAuth token is missing new scopes: {missing}. "
        f"Re-OAuth via /accounts UI or auth.add_account_incremental('{claude_test_account}')."
    )


def test_subfolder_fixture_creates_per_test_folder(claude_test_subfolder, claude_test_account):
    """The claude_test_subfolder fixture should create a fresh subfolder."""
    from src.tools import drive

    meta = drive.get_metadata(claude_test_subfolder, account=claude_test_account)
    assert meta["mimeType"] == "application/vnd.google-apps.folder"
    # Name should contain this test's name + timestamp
    assert "test_subfolder_fixture_creates_per_test_folder" in meta["name"]
