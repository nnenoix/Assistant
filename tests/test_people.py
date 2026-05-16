import pytest

from src import people as people_mod


@pytest.fixture
def fresh_people(tmp_path, monkeypatch):
    monkeypatch.setattr(people_mod, "PEOPLE_FILE", tmp_path / "people.json")


def test_add_first_person(fresh_people):
    p = people_mod.add(account="elena", names=["Лена", "Елена"], email="elena@example.com")
    assert p["id"] == 1
    assert p["account"] == "elena"
    assert p["names"] == ["Лена", "Елена"]


def test_add_merges_into_existing_account(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    people_mod.add(account="elena", names=["Elena Titarenko"], note="бухгалтер")
    everyone = people_mod.list_people()
    assert len(everyone) == 1
    assert "Elena Titarenko" in everyone[0]["names"]
    assert "Лена" in everyone[0]["names"]
    assert everyone[0]["note"] == "бухгалтер"


def test_add_does_not_duplicate_names_case_insensitive(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    people_mod.add(account="elena", names=["лена", "ЛЕНА"])
    p = people_mod.list_people()[0]
    # The first canonical "Лена" survives; case duplicates are dropped.
    assert len(p["names"]) == 1


def test_resolve_exact_name(fresh_people):
    people_mod.add(account="elena", names=["Лена", "Елена"])
    hits = people_mod.resolve("Лена")
    assert len(hits) == 1
    assert hits[0]["account"] == "elena"


def test_resolve_case_insensitive(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    assert people_mod.resolve("лена")[0]["account"] == "elena"


def test_resolve_by_email_local_part(fresh_people):
    people_mod.add(account="elena", names=["Лена"], email="elenatitarenko247@gmail.com")
    hits = people_mod.resolve("elenatitarenko247")
    assert len(hits) == 1


def test_resolve_by_account_alias_exact(fresh_people):
    people_mod.add(account="work", names=["Я работаю"])
    assert people_mod.resolve("work")[0]["account"] == "work"


def test_resolve_no_match_returns_empty(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    assert people_mod.resolve("Pavel") == []


def test_resolve_ambiguous_returns_multiple(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    people_mod.add(account="lena_work", names=["Лена"])
    hits = people_mod.resolve("Лена")
    assert len(hits) == 2


def test_resolve_partial_substring(fresh_people):
    people_mod.add(account="elena", names=["Елена Титаренко"])
    assert people_mod.resolve("Титаренко")[0]["account"] == "elena"


def test_remove_existing(fresh_people):
    people_mod.add(account="elena", names=["Лена"])
    res = people_mod.remove("elena")
    assert res["removed"] is True
    assert people_mod.list_people() == []


def test_remove_missing(fresh_people):
    assert people_mod.remove("nope")["removed"] is False


def test_add_requires_names(fresh_people):
    with pytest.raises(ValueError):
        people_mod.add(account="elena", names=[])
