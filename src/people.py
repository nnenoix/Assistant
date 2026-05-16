"""People registry — maps human names / emails to OAuth account aliases.

Lets the agent infer which account to use from natural language. The user can
say "у Лены в ВБ отчёте" and the agent resolves "Лена" → account="elena"
without the user having to remember the alias.

Stored in `.data/people.json` as a list:
[
  {"id": 1, "names": ["Лена", "Елена"], "email": "elena@example.com", "account": "elena"},
  {"id": 2, "names": ["Я", "main", "Егор"], "email": "egor.titt@gmail.com", "account": "main"}
]

A single entry can bind multiple names (and aliases / typos) to one account.
`account` is the OAuth alias as registered in `.data/tokens/<account>.json`.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


PEOPLE_FILE = DATA_DIR / "people.json"


def _load() -> list[dict[str, Any]]:
    if not PEOPLE_FILE.exists():
        return []
    text = PEOPLE_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _save(people: list[dict[str, Any]]) -> None:
    PEOPLE_FILE.write_text(
        json.dumps(people, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_people() -> list[dict]:
    """All registered people, oldest first."""
    return _load()


def add(
    account: str,
    names: list[str] | str,
    email: str | None = None,
    note: str | None = None,
) -> dict:
    """Register or update a person. If a person with this `account` already
    exists, names/email/note are merged into the existing entry.
    """
    if isinstance(names, str):
        names = [names]
    names = [n.strip() for n in names if n and n.strip()]
    if not account or not names:
        raise ValueError("account and at least one name are required")

    people = _load()
    for p in people:
        if p["account"] == account:
            existing_names = {n.lower(): n for n in p.get("names", [])}
            for n in names:
                if n.lower() not in existing_names:
                    p.setdefault("names", []).append(n)
            if email:
                p["email"] = email
            if note:
                p["note"] = note
            p["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _save(people)
            return p

    entry = {
        "id": (people[-1]["id"] + 1) if people else 1,
        "account": account,
        "names": names,
        "email": email,
        "note": note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    people.append(entry)
    _save(people)
    return entry


def remove(account: str) -> dict:
    people = _load()
    before = len(people)
    people = [p for p in people if p["account"] != account]
    _save(people)
    return {"removed": before - len(people) > 0, "account": account}


def resolve(hint: str) -> list[dict]:
    """Match a free-text hint against names and email. Returns ALL matches —
    if the agent gets >1 result, it should ask the user to disambiguate.

    Match rules:
      - case-insensitive substring on each registered name
      - case-insensitive substring on the local part of the email
      - exact account alias is always a hit
    """
    q = hint.strip().lower()
    if not q:
        return []
    hits: list[dict] = []
    for p in _load():
        if p["account"].lower() == q:
            hits.append(p)
            continue
        matched = False
        for n in p.get("names", []):
            if q in n.lower() or n.lower() in q:
                hits.append(p); matched = True; break
        if matched:
            continue
        email = (p.get("email") or "").lower()
        if email and (q in email or email.split("@")[0] in q):
            hits.append(p)
    # de-duplicate by id while preserving order
    seen: set[int] = set()
    out = []
    for p in hits:
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    return out
