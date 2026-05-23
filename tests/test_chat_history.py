"""Tests for the chat-history persistence layer + HTTP endpoints.

Covers:
  - chats.load_chat_log / rename_chat / delete_chat
  - render_history_for_resume formatting
  - /api/chats list / get / rename / delete endpoints
  - POST /chat with chat_id continues the same file
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_chats_dir(tmp_path, monkeypatch):
    """Redirect CHATS_DIR to a tmp folder so the test doesn't see real chats.
    Also resets the module-level `_chat_log` pointer in src.app so a chat
    leftover from a previous test doesn't bleed in."""
    from src import chats
    monkeypatch.setattr(chats, "CHATS_DIR", tmp_path)
    # Reset the active-chat pointer in app.py — otherwise stale ChatLog
    # objects with paths pointing at the real CHATS_DIR get appended to.
    try:
        from src import app as _app
        monkeypatch.setattr(_app, "_chat_log", None)
    except Exception:
        pass
    return tmp_path


# ============================================================
# ChatLog start / load / append
# ============================================================

def test_start_new_creates_file(fresh_chats_dir):
    from src.chats import ChatLog
    log = ChatLog.start_new()
    assert log.path.exists()
    assert log.data["id"]
    assert log.data["messages"] == []


def test_append_user_sets_title_from_first_message(fresh_chats_dir):
    from src.chats import ChatLog
    log = ChatLog.start_new()
    log.append_user("Покажи остатки WB за май")
    assert log.data["title"] == "Покажи остатки WB за май"
    log.append_user("ещё одно сообщение")
    # Title stays — only set on first user message
    assert log.data["title"] == "Покажи остатки WB за май"


def test_load_chat_log_resumes_existing(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("первое сообщение")
    chat_id = log.data["id"]

    reloaded = chats.load_chat_log(chat_id)
    assert reloaded.data["id"] == chat_id
    assert len(reloaded.data["messages"]) == 1
    # Appending to the reloaded log persists back to the SAME file
    reloaded.append_user("второе сообщение")
    on_disk = chats.read_chat(chat_id)
    assert len(on_disk["messages"]) == 2


def test_load_chat_log_missing_raises(fresh_chats_dir):
    from src import chats
    with pytest.raises(FileNotFoundError):
        chats.load_chat_log("does-not-exist")


# ============================================================
# rename / delete
# ============================================================

def test_rename_chat(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("hi")
    out = chats.rename_chat(log.data["id"], "My renamed chat")
    assert out["ok"] is True
    assert out["title"] == "My renamed chat"
    assert chats.read_chat(log.data["id"])["title"] == "My renamed chat"


def test_rename_chat_missing_returns_error(fresh_chats_dir):
    from src import chats
    out = chats.rename_chat("nope", "title")
    assert out["ok"] is False


def test_rename_chat_rejects_empty_title(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    out = chats.rename_chat(log.data["id"], "   ")
    assert out["ok"] is False


def test_delete_chat_removes_file(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    cid = log.data["id"]
    out = chats.delete_chat(cid)
    assert out["ok"] is True
    assert out["deleted"] is True
    assert not log.path.exists()


def test_delete_chat_missing_is_idempotent(fresh_chats_dir):
    from src import chats
    out = chats.delete_chat("never-existed")
    assert out["ok"] is True
    assert out["deleted"] is False


# ============================================================
# render_history_for_resume
# ============================================================

def test_render_history_for_resume_includes_user_messages(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("первый вопрос")
    log.append_event({"type": "text", "text": "ответ агента"})
    text = chats.render_history_for_resume(log.data["id"])
    assert "[user] первый вопрос" in text
    assert "[assistant] ответ агента" in text
    assert text.startswith("## Previous conversation")


def test_render_history_for_resume_includes_tool_calls(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("задача")
    log.append_event({"type": "tool_call", "name": "drive_resolve_link",
                      "input": {"url": "https://drive.google.com/x"}})
    log.append_event({"type": "tool_result",
                      "result_preview": '[{"type":"text","text":"{\\"ok\\":true}"}]'})
    text = chats.render_history_for_resume(log.data["id"])
    assert "[tool] drive_resolve_link" in text
    assert "[tool_result]" in text


def test_render_history_truncates_long_chats(fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    for i in range(200):
        log.append_user(f"очень длинный вопрос номер {i} {'x'*50}")
        log.append_event({"type": "text", "text": f"ответ {i}"})
    text = chats.render_history_for_resume(log.data["id"], max_chars=2000)
    assert len(text) <= 2100  # some slack for header
    # Tail kept — last user message should be there
    assert "199" in text or "198" in text


def test_render_history_missing_chat_returns_empty(fresh_chats_dir):
    from src import chats
    assert chats.render_history_for_resume("nope") == ""


# ============================================================
# HTTP endpoints
# ============================================================

@pytest.fixture
def client(fresh_chats_dir):
    from fastapi.testclient import TestClient
    from src.app import app
    return TestClient(app)


def test_list_chats_endpoint_empty(client):
    r = client.get("/api/chats")
    assert r.status_code == 200
    assert r.json() == {"chats": []}


def test_list_chats_endpoint_returns_recent(client, fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("hello")
    r = client.get("/api/chats")
    body = r.json()
    assert len(body["chats"]) == 1
    assert body["chats"][0]["title"] == "hello"


def test_get_chat_endpoint_returns_full_history(client, fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("привет")
    log.append_event({"type": "text", "text": "пр1"})
    r = client.get(f"/api/chats/{log.data['id']}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["messages"]) == 2  # user + assistant


def test_get_chat_endpoint_404_on_missing(client):
    r = client.get("/api/chats/nope")
    assert r.status_code == 404


def test_rename_chat_endpoint(client, fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("orig")
    r = client.post(f"/api/chats/{log.data['id']}/rename", json={"title": "Новое название"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Новое название"
    # Persisted
    assert chats.read_chat(log.data["id"])["title"] == "Новое название"


def test_rename_chat_endpoint_empty_title_400(client, fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    log.append_user("orig")
    r = client.post(f"/api/chats/{log.data['id']}/rename", json={"title": ""})
    assert r.status_code == 400


def test_delete_chat_endpoint(client, fresh_chats_dir):
    from src import chats
    log = chats.ChatLog.start_new()
    cid = log.data["id"]
    r = client.delete(f"/api/chats/{cid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


# ============================================================
# POST /chat with chat_id
# ============================================================

def test_post_chat_with_no_chat_id_creates_new(client, fresh_chats_dir):
    """First message of a fresh session should mint a new chat_id and
    return it so the UI can pin to it."""
    with patch("src.app._session") as fake_session:
        # Avoid actually running Claude — stub run_turn to no-op
        async def noop_run(*a, **kw):
            return
        fake_session.run_turn = noop_run
        fake_session.close = lambda: noop_run()
        fake_session._client = None

        r = client.post("/chat", json={"message": "hello there"})
    assert r.status_code == 200
    body = r.json()
    assert body["chat_id"]
    # Check it landed in our tmp dir
    from src import chats
    on_disk = chats.read_chat(body["chat_id"])
    assert on_disk["messages"][0]["text"] == "hello there"


def test_post_chat_with_unknown_chat_id_returns_404(client, fresh_chats_dir):
    r = client.post("/chat", json={"message": "x", "chat_id": "does-not-exist"})
    assert r.status_code == 404


def test_post_chat_with_chat_id_appends_to_same_file(client, fresh_chats_dir):
    """Two messages with the same chat_id must both land in the same .json."""
    from src import chats
    log = chats.ChatLog.start_new()
    cid = log.data["id"]

    async def noop_run(*a, **kw):
        return

    with patch("src.app._session") as fake_session:
        fake_session.run_turn = noop_run
        fake_session.close = noop_run
        fake_session._client = None

        client.post("/chat", json={"message": "msg-A", "chat_id": cid})
        client.post("/chat", json={"message": "msg-B", "chat_id": cid})

    on_disk = chats.read_chat(cid)
    user_msgs = [m for m in on_disk["messages"] if m.get("role") == "user"]
    assert [m["text"] for m in user_msgs] == ["msg-A", "msg-B"]
