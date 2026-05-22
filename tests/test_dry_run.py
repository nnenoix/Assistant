"""Dry-run preview support for destructive tools.

Universal opt-in: every destructive tool exposes `dry_run: bool` in its
schema. When True, native implementations return a structured `plan` dict
describing what WOULD happen; tools without a native impl get a stub
saying "preview not yet implemented, here's the call signature."

Locks down:
  - `dry_run` field shows up on destructive tools only (read-only stay clean)
  - Native impls (drive_delete, sheets_write_range) emit useful preview dicts
  - Stub fallback works on tools that haven't implemented dry_run yet
  - Non-dry-run path (`dry_run=False` or omitted) behaves as before
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from src.tools import registry


def _by_name():
    return {t["name"]: t for t in registry.TOOLS}


# ---------- schema gating ----------

def test_destructive_tool_has_dry_run_schema_field():
    for name in ("drive_delete", "sheets_write_range", "gmail_send_draft"):
        spec = _by_name()[name]
        props = spec["schema"]["input_schema"]["properties"]
        assert "dry_run" in props, f"{name} missing dry_run in schema"
        assert spec["supports_dry_run"] is True


def test_read_only_tool_does_not_get_dry_run_field():
    for name in ("drive_list_files", "sheets_read_range", "gmail_search"):
        spec = _by_name()[name]
        props = spec["schema"]["input_schema"]["properties"]
        assert "dry_run" not in props, f"{name} should not expose dry_run"
        assert spec["supports_dry_run"] is False


# ---------- native impls ----------

def test_drive_delete_dry_run_returns_plan_without_calling_delete():
    """drive.delete with dry_run=True must fetch metadata via files().get
    and return a plan; the real files().delete must NOT be called."""
    from src.tools import drive

    fake_svc = MagicMock()
    fake_svc.files().get().execute.return_value = {
        "id": "FID1",
        "name": "report.xlsx",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "owners": [{"emailAddress": "egor@example.com"}],
        "size": "12345",
        "modifiedTime": "2026-05-22T10:00:00Z",
        "trashed": False,
    }
    with patch.object(drive, "_service", return_value=fake_svc):
        out = drive.delete("FID1", dry_run=True)
    assert out["dry_run"] is True
    assert out["executed"] is False
    assert out["plan"]["name"] == "report.xlsx"
    assert out["plan"]["owner"] == "egor@example.com"
    assert "NOT REVERSIBLE" in out["plan"]["reversibility"]
    # The real delete must not have been called
    fake_svc.files().delete.assert_not_called()


def test_drive_delete_no_dry_run_does_execute():
    """Without dry_run, drive.delete invokes the real delete API."""
    from src.tools import drive

    fake_svc = MagicMock()
    with patch.object(drive, "_service", return_value=fake_svc):
        out = drive.delete("FID2")
    assert out is None  # legacy contract — returns None on real execute
    fake_svc.files().delete.assert_called_with(fileId="FID2")


def test_sheets_write_range_dry_run_returns_plan():
    """sheets.write_range dry_run returns shape + sample without writing."""
    from src.tools import sheets

    fake_svc = MagicMock()
    fake_svc.spreadsheets().values().get().execute.return_value = {
        "values": [["old1", "old2"], ["old3", "old4"]],
    }
    with patch.object(sheets, "_service", return_value=fake_svc):
        out = sheets.write_range(
            "SID", "Sheet1!A1:B2",
            [["new1", "new2"], ["new3", "new4"]],
            dry_run=True,
        )
    assert out["dry_run"] is True
    assert out["executed"] is False
    plan = out["plan"]
    assert plan["would_write_cells"] == 4
    assert plan["shape"] == {"rows": 2, "cols": 2}
    assert plan["current_first_3_rows"] == [["old1", "old2"], ["old3", "old4"]]
    assert "REVERSIBLE" in plan["reversibility"]
    # update() must NOT have been called
    fake_svc.spreadsheets().values().update.assert_not_called()


# ---------- stub fallback for non-native tools ----------

def test_wrapper_returns_stub_for_destructive_tool_without_native_dry_run():
    """A destructive tool that doesn't accept `dry_run` kwarg still advertises
    it in schema; the wrapper must intercept and return a stub envelope
    instead of calling the function."""
    calls = []

    def fn(target: str):  # no `dry_run` param → not native
        calls.append(target)
        return {"deleted": target}

    spec = registry._tool(
        "test_destructive_no_native",
        fn,
        "drive.delete",  # destructive verb
        "test", {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    )
    assert spec["supports_dry_run"] is True
    assert spec["native_dry_run"] is False
    wrapped = registry._wrap_for_sdk(spec)
    handler = getattr(wrapped, "handler", wrapped)
    result = asyncio.run(handler({"target": "X", "dry_run": True}))
    body = json.loads(result["content"][0]["text"])
    assert body["dry_run"] is True
    assert body["executed"] is False
    assert body["tool"] == "test_destructive_no_native"
    assert body["plan"]["would_call"] == "test_destructive_no_native"
    # The real fn must not have been called
    assert calls == []


def test_wrapper_passes_dry_run_through_for_native_impls():
    """When the fn accepts `dry_run` (native), wrapper must pass it through
    instead of returning the stub."""
    received = {}

    def fn(target: str, dry_run: bool = False):
        received["dry_run"] = dry_run
        received["target"] = target
        return {"dry_run": dry_run, "executed": not dry_run, "target": target}

    spec = registry._tool(
        "test_destructive_native",
        fn,
        "drive.delete",
        "test",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    )
    assert spec["native_dry_run"] is True
    wrapped = registry._wrap_for_sdk(spec)
    handler = getattr(wrapped, "handler", wrapped)
    result = asyncio.run(handler({"target": "X", "dry_run": True}))
    body = json.loads(result["content"][0]["text"])
    assert received["dry_run"] is True
    assert body["dry_run"] is True


# ---------- new native impls (R0 destructive: send/delete/clear/edit) ----------

def test_gmail_send_draft_dry_run_returns_recipient_preview():
    """gmail.send_draft dry_run fetches headers (To/Subject/From) and
    returns a preview without calling drafts().send."""
    from src.tools import gmail

    fake_svc = MagicMock()
    fake_svc.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "T1",
            "payload": {"headers": [
                {"name": "To", "value": "boss@example.com"},
                {"name": "Subject", "value": "Q1 report"},
                {"name": "From", "value": "me@example.com"},
            ]},
        },
    }
    with patch.object(gmail, "_service", return_value=fake_svc):
        out = gmail.send_draft("DRAFT_123", dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["to"] == "boss@example.com"
    assert out["plan"]["subject"] == "Q1 report"
    assert "NOT REVERSIBLE" in out["plan"]["reversibility"]
    fake_svc.users().drafts().send.assert_not_called()


def test_sheets_clear_range_dry_run_counts_non_empty_cells():
    """sheets.clear_range dry_run returns non_empty_cells count and a
    current sample so the agent can confirm what's being wiped."""
    from src.tools import sheets

    fake_svc = MagicMock()
    fake_svc.spreadsheets().values().get().execute.return_value = {
        "values": [["a", "b", ""], ["c", "", ""]],
    }
    with patch.object(sheets, "_service", return_value=fake_svc):
        out = sheets.clear_range("SID", "Sheet1!A1:C2", dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["non_empty_cells"] == 3  # a, b, c
    assert "REVERSIBLE" in out["plan"]["reversibility"]
    fake_svc.spreadsheets().values().clear.assert_not_called()


def test_calendar_delete_event_dry_run_includes_attendees():
    """calendar.delete_event dry_run fetches event metadata + attendees."""
    from src.tools import calendar as cal

    fake_svc = MagicMock()
    fake_svc.events().get().execute.return_value = {
        "summary": "Sync",
        "start": {"dateTime": "2026-05-22T10:00:00+03:00"},
        "end": {"dateTime": "2026-05-22T11:00:00+03:00"},
        "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}],
        "organizer": {"email": "me@x.com"},
    }
    with patch.object(cal, "_service", return_value=fake_svc):
        out = cal.delete_event("EV_1", dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["summary"] == "Sync"
    assert out["plan"]["attendees"] == ["a@x.com", "b@x.com"]
    fake_svc.events().delete.assert_not_called()


def test_apps_script_edit_file_dry_run_reports_delta():
    """edit_file dry_run shows old vs new bytes + would-be action."""
    from src.tools import apps_script_api

    with patch.object(apps_script_api, "get_content", return_value={
        "files": [
            {"name": "Code", "type": "SERVER_JS", "source": "old source 12345"},
            {"name": "appsscript", "type": "JSON", "source": "{}"},
        ],
    }), patch.object(apps_script_api, "update_content") as mock_update:
        out = apps_script_api.edit_file("S1", "Code", "new source 999", dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["action"] == "replaced"
    assert out["plan"]["old_bytes"] == len("old source 12345")
    assert out["plan"]["new_bytes"] == len("new source 999")
    assert out["plan"]["delta_bytes"] == len("new source 999") - len("old source 12345")
    mock_update.assert_not_called()


def test_apps_script_edit_file_dry_run_detects_create_path():
    """When the file doesn't exist, dry_run reports action='created'."""
    from src.tools import apps_script_api

    with patch.object(apps_script_api, "get_content", return_value={"files": []}), \
         patch.object(apps_script_api, "update_content") as mock_update:
        out = apps_script_api.edit_file("S1", "NewFile", "hello", dry_run=True)
    assert out["plan"]["action"] == "created"
    assert out["plan"]["old_bytes"] == 0
    mock_update.assert_not_called()


def test_contacts_delete_dry_run_shows_name_and_emails():
    """contacts.delete dry_run fetches displayName + emails so the agent
    can confirm the right contact is about to disappear."""
    from src.tools import contacts

    fake_svc = MagicMock()
    fake_svc.people().get().execute.return_value = {
        "names": [{"displayName": "Иван Иванов"}],
        "emailAddresses": [{"value": "ivan@example.com"}],
        "phoneNumbers": [{"value": "+71234567890"}],
    }
    with patch.object(contacts, "_service", return_value=fake_svc):
        out = contacts.delete("people/c123", dry_run=True)
    assert out["dry_run"] is True
    assert out["plan"]["display_name"] == "Иван Иванов"
    assert out["plan"]["emails"] == ["ivan@example.com"]
    assert "NOT REVERSIBLE" in out["plan"]["reversibility"]
    fake_svc.people().deleteContact.assert_not_called()


def test_wrapper_no_dry_run_in_args_executes_normally():
    """If dry_run is absent or False, the wrapper executes the fn normally."""
    calls = []

    def fn(target: str):
        calls.append(target)
        return {"ok": True}

    spec = registry._tool(
        "test_destructive_normal",
        fn,
        "drive.delete",
        "test",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    )
    wrapped = registry._wrap_for_sdk(spec)
    handler = getattr(wrapped, "handler", wrapped)
    asyncio.run(handler({"target": "Y"}))
    assert calls == ["Y"]
