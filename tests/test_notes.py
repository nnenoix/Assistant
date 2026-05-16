import pytest

from src.tools import notes


@pytest.fixture
def fresh_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_FILE", tmp_path / "notes.json")


def test_add_assigns_incrementing_ids(fresh_notes):
    a = notes.add("first")
    b = notes.add("second")
    assert a["id"] == 1 and b["id"] == 2


def test_add_with_tag(fresh_notes):
    n = notes.add("Лена НДС 5% в 2026", tag="elena")
    assert n["tag"] == "elena"
    assert n["ts"]


def test_list_returns_in_insertion_order(fresh_notes):
    for t in ["a", "b", "c"]:
        notes.add(t)
    out = notes.list_notes()
    assert [n["text"] for n in out] == ["a", "b", "c"]


def test_search_matches_text(fresh_notes):
    notes.add("ИД презентации = 1AbC")
    notes.add("Просто заметка")
    found = notes.search("презентации")
    assert len(found) == 1


def test_search_matches_tag_case_insensitive(fresh_notes):
    notes.add("hi", tag="Elena")
    found = notes.search("elena")
    assert len(found) == 1


def test_search_empty_query_returns_empty(fresh_notes):
    notes.add("anything")
    assert notes.search("") == []
    assert notes.search("   ") == []


def test_remove_existing(fresh_notes):
    a = notes.add("doomed")
    res = notes.remove(a["id"])
    assert res["removed"] is True
    assert notes.list_notes() == []


def test_remove_missing(fresh_notes):
    assert notes.remove(999)["removed"] is False


def test_missing_file_treated_as_empty(fresh_notes):
    assert notes.list_notes() == []
    assert notes.search("anything") == []
