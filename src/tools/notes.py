"""Persistent notes — the agent's long-term memory.

Stored in `.data/notes.json`. The agent can drop short facts here ("Лена 2026
НДС 5%", "ID последней презентации = 1AbC…") and recall them in future turns
via search.
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


NOTES_FILE = DATA_DIR / "notes.json"

# Serialise read+modify+write across threads. Phase 15's file_analyze.analyze
# can be invoked concurrently from multiple ensemble calls — without this lock,
# two simultaneous notes.add() could race on id assignment AND lose entries
# (last writer wins on _save).
_NOTES_LOCK = threading.Lock()


def _load() -> list[dict[str, Any]]:
    if not NOTES_FILE.exists():
        return []
    text = NOTES_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _save(notes: list[dict[str, Any]]) -> None:
    # Atomic write via temp + rename so a kill mid-write doesn't corrupt notes.json
    tmp = NOTES_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(notes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(NOTES_FILE)


def add(text: str, tag: str | None = None) -> dict:
    """Add a new note. Returns the saved entry with assigned id and timestamp.

    Thread-safe: read+modify+write is serialised under _NOTES_LOCK so concurrent
    callers don't collide on id assignment.
    """
    with _NOTES_LOCK:
        notes = _load()
        next_id = (notes[-1]["id"] + 1) if notes else 1
        entry = {
            "id": next_id,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "tag": tag,
        }
        notes.append(entry)
        _save(notes)
        return entry


def list_notes(limit: int = 50) -> list[dict]:
    """Return the most recent `limit` notes, newest last."""
    return _load()[-limit:]


def search(query: str) -> list[dict]:
    """Substring match across text and tag (case-insensitive)."""
    q = query.lower().strip()
    if not q:
        return []
    return [
        n for n in _load()
        if q in n.get("text", "").lower()
        or (n.get("tag") and q in n["tag"].lower())
    ]


def remove(id: int) -> dict:
    """Delete a note by id. Returns whether removal happened.
    Thread-safe under _NOTES_LOCK (paired with add())."""
    with _NOTES_LOCK:
        notes = _load()
        before = len(notes)
        notes = [n for n in notes if n.get("id") != id]
        _save(notes)
        return {"removed": before - len(notes) > 0, "id": id}


def search_semantic(query: str, top_k: int = 8) -> dict:
    """Semantic search across notes using local embeddings. Falls back to
    substring search if the embedding model isn't available.

    Returns {results, _meta:{search_method}} so the agent knows whether it
    got actual semantic ranking or a substring fallback (`scores` are
    omitted in the fallback case).
    """
    from src import embeddings

    notes = _load()
    if not notes:
        return {
            "results": [],
            "_meta": {"search_method": "semantic", "empty_reason": "no_notes"},
        }

    embeddings.upsert(
        scope="notes",
        items=[
            {"key": str(n["id"]), "text": n["text"] + (f"  [{n['tag']}]" if n.get("tag") else ""), "meta": n}
            for n in notes
        ],
    )
    embeddings.purge(scope="notes", keep_keys={str(n["id"]) for n in notes})

    hits = embeddings.query(scope="notes", text=query, top_k=top_k)
    if not hits:
        results = search(query)
        return {
            "results": results,
            "_meta": {
                "search_method": "substring",
                "fallback_reason": "embeddings unavailable or returned no hits",
                "empty_reason": None if results else "no_matches",
            },
        }
    return {
        "results": [{"score": round(h["score"], 4), **h["meta"]} for h in hits],
        "_meta": {"search_method": "semantic", "empty_reason": None},
    }
