"""Tests for `drive.resolve_link` — the multi-account link resolver
that makes "паст ссылку → агент сам разберётся" actually work.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.tools import drive


# ============================================================
# Happy path: one account sees the link
# ============================================================

def test_resolve_link_finds_accessible_account():
    """Three accounts: only `elena` sees the file. Resolver returns
    recommended_account='elena' + the metadata."""
    from googleapiclient.errors import HttpError
    from unittest.mock import MagicMock

    def fake_get_metadata(file_id, account="main"):
        if account == "elena":
            return {"id": file_id, "name": "Shared Doc",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "https://docs.google.com/document/d/X/edit"}
        # Other accounts get 404
        resp = MagicMock()
        resp.status = 404
        raise HttpError(resp=resp, content=b'{"error":"not found"}')

    with patch.object(drive, "get_metadata", side_effect=fake_get_metadata), \
         patch("src.auth.list_accounts", return_value=["egor", "elena", "work"]):
        out = drive.resolve_link("https://drive.google.com/file/d/X/view")

    assert out["ok"] is True
    assert out["parsed"]["kind"] == "file"
    assert out["parsed"]["id"] == "X"
    assert out["accessible_via"] == ["elena"]
    assert set(out["not_seen_by"]) == {"egor", "work"}
    assert out["recommended_account"] == "elena"
    assert out["metadata"]["name"] == "Shared Doc"


def test_resolve_link_picks_first_accessible_when_multiple_work():
    """Both egor and elena see the folder — first one (in list order)
    wins as `recommended_account`."""
    def fake_get_metadata(file_id, account="main"):
        return {"id": file_id, "name": "Public",
                "mimeType": "application/vnd.google-apps.folder"}

    with patch.object(drive, "get_metadata", side_effect=fake_get_metadata), \
         patch("src.auth.list_accounts", return_value=["egor", "elena"]):
        out = drive.resolve_link(
            "https://drive.google.com/drive/folders/1BP6m-gcgAo2EY3V1JSo_2d1jtgvYEZ1-"
        )
    assert out["accessible_via"] == ["egor", "elena"]
    assert out["recommended_account"] == "egor"


# ============================================================
# Failure paths
# ============================================================

def test_resolve_link_rejects_non_drive_url():
    out = drive.resolve_link("https://example.com/some/page")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert out["parsed"]["kind"] == "unknown"


def test_resolve_link_no_accounts_returns_auth_scope():
    """First-run case: user hasn't OAuth'd anyone yet."""
    with patch("src.auth.list_accounts", return_value=[]):
        out = drive.resolve_link("https://drive.google.com/drive/folders/F1")
    assert out["ok"] is False
    assert out["error_kind"] == "auth_scope"
    assert out["suggestion"] == "add_account"


def test_resolve_link_nobody_sees_it_returns_not_found():
    """All accounts return 404 — link probably wrong OR user needs
    to add the right account."""
    from googleapiclient.errors import HttpError
    from unittest.mock import MagicMock

    def all_404(file_id, account="main"):
        resp = MagicMock()
        resp.status = 404
        raise HttpError(resp=resp, content=b'{"error":"not found"}')

    with patch.object(drive, "get_metadata", side_effect=all_404), \
         patch("src.auth.list_accounts", return_value=["egor", "elena"]):
        out = drive.resolve_link("https://drive.google.com/drive/folders/F1")

    assert out["ok"] is False
    assert out["error_kind"] == "not_found"
    assert out["accessible_via"] == []
    assert set(out["not_seen_by"]) == {"egor", "elena"}
    assert out["suggestion"] == "add_account"
    assert "add" in out["hint"].lower()


def test_resolve_link_permission_denied_dominates_404():
    """If at least ONE account got 403 (permission), the dominant
    error_kind is `permission`, not `not_found` — user is closer than
    they think, they just need to ask the owner for access."""
    from googleapiclient.errors import HttpError
    from unittest.mock import MagicMock

    def mixed(file_id, account="main"):
        resp = MagicMock()
        if account == "egor":
            resp.status = 403
            raise HttpError(resp=resp, content=b'{"error":"forbidden"}')
        resp.status = 404
        raise HttpError(resp=resp, content=b'{"error":"not found"}')

    with patch.object(drive, "get_metadata", side_effect=mixed), \
         patch("src.auth.list_accounts", return_value=["egor", "elena"]):
        out = drive.resolve_link("https://drive.google.com/drive/folders/F1")

    assert out["error_kind"] == "permission"
    assert "ask the owner" in out["hint"].lower() or "denied" in out["hint"].lower()


# ============================================================
# Explicit accounts override
# ============================================================

def test_resolve_link_accepts_explicit_accounts_list():
    """Caller can narrow the probe set instead of probing ALL registered."""
    probed: list[str] = []

    def tracking(file_id, account="main"):
        probed.append(account)
        return {"id": file_id, "name": "x", "mimeType": "x"}

    with patch.object(drive, "get_metadata", side_effect=tracking), \
         patch("src.auth.list_accounts", return_value=["a", "b", "c"]):
        drive.resolve_link("https://drive.google.com/file/d/F/view",
                           accounts=["b"])
    assert probed == ["b"]


# ============================================================
# Tool registration
# ============================================================

def test_drive_resolve_link_is_registered():
    from src.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "drive_resolve_link" in names


def test_drive_resolve_link_has_drive_read_policy():
    from src.tools.registry import TOOLS
    spec = next(t for t in TOOLS if t["name"] == "drive_resolve_link")
    assert spec["policy_op"] == "drive.read"
    # Description must signal the agent that this should be called FIRST
    assert "FIRST" in spec["schema"]["description"]


# ============================================================
# System prompt alignment
# ============================================================

def test_system_prompt_has_link_resolve_rule():
    """The agent's behavior depends on rule 23a being present — if a
    later edit removes it, this test catches the regression."""
    import re
    from src.agent import SYSTEM_PROMPT
    assert "drive_resolve_link" in SYSTEM_PROMPT
    assert re.search(r"\b23a\b", SYSTEM_PROMPT), (
        "Rule 23a (drive link resolution) is missing from SYSTEM_PROMPT"
    )
