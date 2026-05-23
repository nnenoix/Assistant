"""Tests for the Drive-UI-fallback tools in src/tools/browser.py.

The actual Playwright path requires a real Chromium + a logged-in
profile — not exercised here. These tests cover:
  - `_parse_drive_url` URL → (kind, id) mapping (pure function, fast).
  - Registry wiring (both new tools are findable).
  - Error envelope shape on early-exit paths (we monkeypatch
    `_launch_persistent` to simulate the failure modes).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools import browser


# ============================================================
# _parse_drive_url
# ============================================================

@pytest.mark.parametrize("url,kind,id_", [
    # Folder share-links — the user's actual ask
    ("https://drive.google.com/drive/folders/1BP6m-gcgAo2EY3V1JSo_2d1jtgvYEZ1-",
     "folder", "1BP6m-gcgAo2EY3V1JSo_2d1jtgvYEZ1-"),
    ("https://drive.google.com/drive/folders/abc123?usp=sharing",
     "folder", "abc123"),
    ("https://drive.google.com/drive/u/0/folders/xyz789",
     "folder", "xyz789"),
    # File links
    ("https://drive.google.com/file/d/FILE_ID_42/view?usp=drive_link",
     "file", "FILE_ID_42"),
    ("https://drive.google.com/file/u/0/d/another_file/preview",
     "file", "another_file"),
    # Docs editors
    ("https://docs.google.com/document/d/DOC1/edit",
     "document", "DOC1"),
    ("https://docs.google.com/spreadsheets/d/SHEET1/edit#gid=0",
     "spreadsheet", "SHEET1"),
    ("https://docs.google.com/presentation/d/SLIDE1/edit",
     "presentation", "SLIDE1"),
    ("https://docs.google.com/forms/d/FORM1/edit",
     "forms", "FORM1"),
])
def test_parse_drive_url_recognizes_supported_shapes(url, kind, id_):
    out = browser._parse_drive_url(url)
    assert out["kind"] == kind
    assert out["id"] == id_


@pytest.mark.parametrize("url", [
    "https://google.com",
    "https://drive.google.com/",
    "https://drive.google.com/drive/my-drive",
    "https://example.com/folders/something",
    "",
    "not a url",
])
def test_parse_drive_url_returns_unknown_for_unrecognized(url):
    out = browser._parse_drive_url(url)
    assert out["kind"] == "unknown"
    assert out["id"] is None


# ============================================================
# drive_open — early-exit paths via monkeypatching the browser launcher
# ============================================================

@pytest.fixture
def fake_browser_ctx(monkeypatch):
    """Return mocks for (pw, ctx, page) so tests can drive the UI state
    without spawning a real Chromium."""
    page = MagicMock()
    page.title.return_value = ""
    page.evaluate.return_value = ""
    page.url = "https://drive.google.com/drive/folders/FOLDER_ID"

    ctx = MagicMock()
    ctx.new_page.return_value = page

    pw = MagicMock()
    monkeypatch.setattr(browser, "_launch_persistent",
                        lambda headless, profile="default": (pw, ctx, "msedge"))
    return {"pw": pw, "ctx": ctx, "page": page}


def test_drive_open_returns_granted_envelope_on_normal_page(fake_browser_ctx):
    p = fake_browser_ctx["page"]
    p.title.return_value = "Test Folder — Google Drive"
    p.evaluate.return_value = "Test Folder\nfile1.txt\nfile2.png"
    p.url = "https://drive.google.com/drive/folders/FOLDER_ID"

    out = browser.drive_open(
        "https://drive.google.com/drive/folders/FOLDER_ID",
        profile="default",
    )
    assert out["ok"] is True
    assert out["access"] == "granted"
    assert out["parsed"]["kind"] == "folder"
    assert out["parsed"]["id"] == "FOLDER_ID"
    assert "Test Folder" in out["title"]
    assert "file1.txt" in out["page_text_preview"]


def test_drive_open_detects_login_redirect(fake_browser_ctx):
    p = fake_browser_ctx["page"]
    # After navigation, page.url is the Google login URL
    p.url = "https://accounts.google.com/v3/signin/identifier"

    out = browser.drive_open(
        "https://drive.google.com/drive/folders/FOLDER_ID",
        profile="elena",
    )
    assert out["ok"] is False
    assert out["access"] == "login_required"
    assert out["error_kind"] == "auth_scope"
    assert "browser_login_interactive(profile='elena')" in out["fix_hint"]


def test_drive_open_detects_permission_denied_interstitial(fake_browser_ctx):
    p = fake_browser_ctx["page"]
    p.url = "https://drive.google.com/drive/folders/FOLDER_ID"
    p.title.return_value = "Запросить доступ"
    p.evaluate.return_value = (
        "Запросить доступ к этой папке\nВладельцу будет отправлено уведомление"
    )

    out = browser.drive_open(
        "https://drive.google.com/drive/folders/FOLDER_ID",
    )
    assert out["ok"] is False
    assert out["access"] == "permission_denied"
    assert out["error_kind"] == "permission"


def test_drive_open_detects_english_request_access(fake_browser_ctx):
    p = fake_browser_ctx["page"]
    p.evaluate.return_value = (
        "You need access\nAsk for access, or switch to an account that has access."
    )
    out = browser.drive_open("https://drive.google.com/drive/folders/F")
    assert out["access"] == "permission_denied"


def test_drive_open_handles_launch_failure(monkeypatch):
    """If Playwright can't launch any browser channel, the wrapper must
    return a clean error envelope (not raise)."""
    def boom(headless, profile="default"):
        raise RuntimeError("no msedge, no chrome, no chromium")
    monkeypatch.setattr(browser, "_launch_persistent", boom)

    out = browser.drive_open("https://drive.google.com/drive/folders/X")
    assert out["ok"] is False
    assert "browser launch" in out["error"]


# ============================================================
# drive_list_folder
# ============================================================

def test_drive_list_folder_rejects_non_folder_url():
    out = browser.drive_list_folder(
        "https://docs.google.com/document/d/DOC1/edit",
    )
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert "not a folder" in out["error"]


def test_drive_list_folder_returns_items(fake_browser_ctx):
    p = fake_browser_ctx["page"]
    p.url = "https://drive.google.com/drive/folders/FOLDER_ID"
    # First evaluate is for `document.querySelectorAll('[data-id]').length`
    # (scroll loop), then the big extraction script.
    p.evaluate.side_effect = [
        2,  # count
        2,  # count again — stable, exits loop
        # The extraction script result
        [
            {"id": "FILE1", "name": "Quarterly Report.xlsx", "kind": "spreadsheet"},
            {"id": "FILE2", "name": "Notes.docx", "kind": "document"},
        ],
    ]

    out = browser.drive_list_folder(
        "https://drive.google.com/drive/folders/FOLDER_ID",
    )
    assert out["ok"] is True
    assert len(out["items"]) == 2
    assert out["items"][0]["id"] == "FILE1"
    assert out["items"][0]["kind"] == "spreadsheet"
    assert out["_meta"]["count"] == 2


def test_drive_list_folder_handles_empty_folder(fake_browser_ctx):
    """No `[data-id]` selector appears + page text isn't an
    access-denied — treat as empty folder, not error."""
    p = fake_browser_ctx["page"]
    p.url = "https://drive.google.com/drive/folders/EMPTY"
    p.wait_for_selector.side_effect = Exception("timeout waiting for selector")
    p.evaluate.return_value = "No files in this folder."

    out = browser.drive_list_folder(
        "https://drive.google.com/drive/folders/EMPTY",
    )
    assert out["ok"] is True
    assert out["items"] == []
    assert "empty folder" in out["_meta"]["reason"]


# ============================================================
# Registry wiring
# ============================================================

def test_browser_drive_tools_are_registered():
    from src.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "drive_browser_open" in names
    assert "drive_browser_list_folder" in names


def test_browser_drive_tools_carry_drive_read_policy():
    """Tools that READ Drive data must be tagged drive.read so RBAC
    rules treat them the same as drive_list_files / drive_get_metadata."""
    from src.tools.registry import TOOLS
    for t in TOOLS:
        if t["name"] in ("drive_browser_open", "drive_browser_list_folder"):
            assert t["policy_op"] == "drive.read", (
                f"{t['name']} must have policy_op='drive.read', got {t['policy_op']!r}"
            )
