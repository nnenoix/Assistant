"""Unit tests for src/tools/gmail.py.

Mocks the Gmail service via the same pattern as test_drive_adapter.py:
patch._service to return a MagicMock chain, then assert on the Google API
calls our wrappers translate to.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import gmail


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(gmail, "_service", return_value=svc):
        yield svc


# ---------- search (already existed; just sanity check) ----------

def test_search_returns_meta_envelope(fake_service):
    fake_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1"}],
        "resultSizeEstimate": 1,
    }
    fake_service.users().messages().get().execute.return_value = {
        "id": "m1", "threadId": "t1", "snippet": "...",
        "payload": {"headers": [{"name": "From", "value": "x@y.com"}]},
        "labelIds": ["INBOX"],
    }
    result = gmail.search("test")
    assert "messages" in result and "_meta" in result
    assert result["_meta"]["returned_count"] == 1


# ---------- Phase 5: get_thread ----------

def test_get_thread_extracts_messages(fake_service):
    fake_service.users().threads().get().execute.return_value = {
        "messages": [
            {
                "id": "m1", "snippet": "hi",
                "payload": {"headers": [{"name": "From", "value": "a@b.com"},
                                         {"name": "Subject", "value": "hello"}],
                            "parts": [{"mimeType": "text/plain", "body": {"data": "aGVsbG8="}}]},
                "labelIds": ["INBOX"],
            },
            {
                "id": "m2", "snippet": "reply",
                "payload": {"headers": [{"name": "From", "value": "b@a.com"},
                                         {"name": "Subject", "value": "Re: hello"}]},
                "labelIds": [],
            },
        ],
    }
    result = gmail.get_thread("t1")
    assert result["_meta"]["message_count"] == 2
    assert result["messages"][0]["from"] == "a@b.com"
    # The base64 of 'hello' should decode in body_text
    assert "hello" in result["messages"][0]["body_text"]


def test_get_thread_empty_flag(fake_service):
    fake_service.users().threads().get().execute.return_value = {"messages": []}
    result = gmail.get_thread("t-empty")
    assert result["_meta"]["empty_reason"] == "no_messages"


# ---------- reply ----------

def test_reply_sets_threading_headers(fake_service):
    fake_service.users().messages().get().execute.return_value = {
        "threadId": "t1",
        "payload": {"headers": [
            {"name": "From", "value": "sender@x.com"},
            {"name": "To", "value": "me@me.com"},
            {"name": "Cc", "value": "boss@x.com"},
            {"name": "Subject", "value": "Important"},
            {"name": "Message-ID", "value": "<original@x.com>"},
            {"name": "References", "value": "<root@x.com>"},
        ]},
    }
    fake_service.users().drafts().create().execute.return_value = {
        "id": "draft-1",
        "message": {"id": "m-new", "threadId": "t1"},
    }
    result = gmail.reply("m-orig", "пишу в ответ", reply_all=False)
    assert result["draft_id"] == "draft-1"
    assert result["subject"] == "Re: Important"
    assert result["thread_id"] == "t1"
    # Inspect the draft body raw to verify In-Reply-To / References headers
    body = fake_service.users().drafts().create.call_args.kwargs["body"]
    import base64
    raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode("utf-8")
    # MIMEText lowercases header names — match insensitively
    assert "In-Reply-To: <original@x.com>" in raw
    assert "References:" in raw or "references:" in raw
    assert "subject: re: important" in raw.lower()
    # threadId echoed into draft so Gmail keeps the conversation
    assert body["message"]["threadId"] == "t1"


def test_reply_all_combines_to_and_cc(fake_service):
    fake_service.users().messages().get().execute.return_value = {
        "threadId": "t",
        "payload": {"headers": [
            {"name": "From", "value": "alice@x.com"},
            {"name": "To", "value": "me@me.com, charlie@x.com"},
            {"name": "Cc", "value": "dave@x.com"},
            {"name": "Subject", "value": "X"},
            {"name": "Message-ID", "value": "<x@x>"},
        ]},
    }
    fake_service.users().drafts().create().execute.return_value = {
        "id": "d", "message": {"id": "m", "threadId": "t"},
    }
    gmail.reply("m-orig", "ok", reply_all=True)
    import base64
    body = fake_service.users().drafts().create.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode("utf-8")
    # Cc should be present and include the other original recipients
    assert "cc:" in raw.lower()
    assert "charlie@x.com" in raw or "dave@x.com" in raw


def test_reply_subject_doesnt_double_re(fake_service):
    fake_service.users().messages().get().execute.return_value = {
        "threadId": "t",
        "payload": {"headers": [
            {"name": "From", "value": "x@y.com"},
            {"name": "Subject", "value": "Re: already a reply"},
            {"name": "Message-ID", "value": "<x@x>"},
        ]},
    }
    fake_service.users().drafts().create().execute.return_value = {
        "id": "d", "message": {"id": "m", "threadId": "t"},
    }
    result = gmail.reply("m", "ok")
    assert result["subject"] == "Re: already a reply"


# ---------- forward ----------

def test_forward_includes_original_body_and_quoted_header(fake_service):
    fake_service.users().messages().get().execute.return_value = {
        "id": "orig",
        "payload": {
            "headers": [
                {"name": "From", "value": "elena@x.com"},
                {"name": "Date", "value": "2026-05-20"},
                {"name": "Subject", "value": "Quarterly"},
                {"name": "To", "value": "me@me.com"},
            ],
            "parts": [{"mimeType": "text/plain", "body": {"data": "b3JpZ2luYWw="}}],  # 'original'
        },
    }
    fake_service.users().drafts().create().execute.return_value = {"id": "d-fwd"}
    result = gmail.forward("orig", "elena@new.com", body="смотри")
    assert result["subject"] == "Fwd: Quarterly"
    # The MIMEText body is base64-encoded inside the urlsafe-b64 wrapper.
    # Parse it back to a real email.message.Message to inspect content.
    import base64
    from email import message_from_bytes
    body = fake_service.users().drafts().create.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    msg_obj = message_from_bytes(raw)
    payload = msg_obj.get_payload(decode=True).decode("utf-8")
    assert "Forwarded message" in payload
    assert "original" in payload
    assert "смотри" in payload


# ---------- modify_labels / archive / mark_read / mark_unread ----------

def test_modify_labels_requires_add_or_remove(fake_service):
    with pytest.raises(ValueError, match="must pass"):
        gmail.modify_labels("m1")


def test_modify_labels_add_remove_both(fake_service):
    fake_service.users().messages().modify().execute.return_value = {"labelIds": ["INBOX", "STARRED"]}
    result = gmail.modify_labels("m1", add=["STARRED"], remove=["UNREAD"])
    body = fake_service.users().messages().modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]}
    assert result["labels_after"] == ["INBOX", "STARRED"]


def test_archive_removes_inbox(fake_service):
    fake_service.users().messages().modify().execute.return_value = {"labelIds": []}
    gmail.archive("m1")
    body = fake_service.users().messages().modify.call_args.kwargs["body"]
    assert body == {"removeLabelIds": ["INBOX"]}


def test_mark_read_removes_unread(fake_service):
    fake_service.users().messages().modify().execute.return_value = {"labelIds": []}
    gmail.mark_read("m1")
    body = fake_service.users().messages().modify.call_args.kwargs["body"]
    assert body == {"removeLabelIds": ["UNREAD"]}


def test_mark_unread_adds_unread(fake_service):
    fake_service.users().messages().modify().execute.return_value = {"labelIds": ["UNREAD"]}
    gmail.mark_unread("m1")
    body = fake_service.users().messages().modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["UNREAD"]}


# ---------- batch_modify ----------

def test_batch_modify_empty_list_short_circuits(fake_service):
    result = gmail.batch_modify([], add=["STARRED"])
    assert result["count"] == 0
    assert result["empty_reason"] == "no_ids"
    fake_service.users().messages().batchModify.assert_not_called()


def test_batch_modify_sends_correct_body(fake_service):
    fake_service.users().messages().batchModify().execute.return_value = {}
    result = gmail.batch_modify(["m1", "m2", "m3"], remove=["INBOX"])
    body = fake_service.users().messages().batchModify.call_args.kwargs["body"]
    assert body["ids"] == ["m1", "m2", "m3"]
    assert body["removeLabelIds"] == ["INBOX"]
    assert result["count"] == 3


def test_batch_modify_requires_action(fake_service):
    with pytest.raises(ValueError, match="must pass"):
        gmail.batch_modify(["m1"])


# ---------- filters ----------

def test_list_filters_returns_meta(fake_service):
    fake_service.users().settings().filters().list().execute.return_value = {
        "filter": [{"id": "f1", "criteria": {"from": "x@y.com"}, "action": {}}],
    }
    result = gmail.list_filters()
    assert result["_meta"]["count"] == 1
    assert result["filters"][0]["id"] == "f1"


def test_create_filter_builds_body(fake_service):
    fake_service.users().settings().filters().create().execute.return_value = {"id": "f-new"}
    result = gmail.create_filter(
        criteria={"from": "noreply@github.com"},
        add_labels=["Label_GitHub"],
        remove_labels=["INBOX"],
    )
    body = fake_service.users().settings().filters().create.call_args.kwargs["body"]
    assert body["criteria"] == {"from": "noreply@github.com"}
    assert body["action"] == {"addLabelIds": ["Label_GitHub"], "removeLabelIds": ["INBOX"]}
    assert result["filter_id"] == "f-new"


def test_create_filter_requires_action(fake_service):
    with pytest.raises(ValueError, match="at least one action"):
        gmail.create_filter(criteria={"from": "x@y.com"})


def test_delete_filter_calls_api(fake_service):
    fake_service.users().settings().filters().delete().execute.return_value = None
    gmail.delete_filter("f-1")
    fake_service.users().settings().filters().delete.assert_called_with(userId="me", id="f-1")
