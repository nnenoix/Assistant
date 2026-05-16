import json

import pytest

from src import chats as chats_mod


@pytest.fixture
def fresh_chats_dir(tmp_path, monkeypatch):
    d = tmp_path / "chats"
    d.mkdir()
    monkeypatch.setattr(chats_mod, "CHATS_DIR", d)
    return d


def test_start_new_creates_file(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    assert log.path.exists()
    data = json.loads(log.path.read_text(encoding="utf-8"))
    assert data["messages"] == []
    assert data["title"] is None


def test_append_user_sets_title_from_first_message(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    log.append_user("найди все таблицы Лены")
    log.append_user("второе сообщение")
    data = json.loads(log.path.read_text(encoding="utf-8"))
    assert data["title"] == "найди все таблицы Лены"
    assert [m["role"] for m in data["messages"]] == ["user", "user"]


def test_append_event_groups_under_assistant(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    log.append_user("hi")
    log.append_event({"type": "text", "text": "Привет"})
    log.append_event({"type": "tool_call", "name": "drive_search", "input": {"name_contains": "x"}})
    log.append_event({"type": "tool_result", "result_preview": "[]"})
    data = json.loads(log.path.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 2
    asst = data["messages"][1]
    assert asst["role"] == "assistant"
    assert len(asst["events"]) == 3


def test_done_event_not_persisted(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    log.append_user("hi")
    log.append_event({"type": "text", "text": "x"})
    log.append_event({"type": "done"})
    data = json.loads(log.path.read_text(encoding="utf-8"))
    assert len(data["messages"][1]["events"]) == 1


def test_list_and_read(fresh_chats_dir):
    log1 = chats_mod.ChatLog.start_new()
    log1.append_user("first chat about Тумалаева")
    listed = chats_mod.list_chats()
    assert len(listed) == 1
    assert "Тумалаева" in listed[0]["title"]

    fetched = chats_mod.read_chat(log1.data["id"])
    assert fetched["title"] == listed[0]["title"]


def test_search_finds_substring_in_user_text(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    log.append_user("работа с таблицей ДДС 2026 ИП Варычев")
    results = chats_mod.search_chats("Варычев")
    assert len(results) == 1
    assert any("Варычев" in s["snippet"] for s in results[0]["matches"])


def test_search_finds_substring_in_assistant_tool_call(fresh_chats_dir):
    log = chats_mod.ChatLog.start_new()
    log.append_user("hi")
    log.append_event({"type": "tool_call", "name": "drive_search", "input": {"name_contains": "idealnight"}})
    log.append_event({"type": "tool_result", "result_preview": '[{"name": "idealnight v3"}]'})
    results = chats_mod.search_chats("idealnight")
    assert len(results) == 1


def test_read_chat_missing_raises(fresh_chats_dir):
    with pytest.raises(FileNotFoundError):
        chats_mod.read_chat("nonexistent")
