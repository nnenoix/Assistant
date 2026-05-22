"""Agent-facing tools for reading saved chat history.

Persistence happens automatically in src/chats.py — these are the read side.
"""
from src import chats as _impl


def list_recent(limit: int = 30) -> list[dict]:
    """List recent chats, newest first. Each entry has id, title, started_at, message_count."""
    return _impl.list_chats(limit=limit)


def read(chat_id: str) -> dict:
    """Read the full conversation for a given chat id."""
    return _impl.read_chat(chat_id)


def search(query: str, limit: int = 10, response_format: str = "concise") -> dict:
    """Substring search across all saved chats. Returns matches with snippets.

    `response_format`:
      - "concise" (default): per-match `{chat_id, title, snippet[:200]}`.
      - "detailed": adds full `snippet`, `started_at`, `message_count`.
    """
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    matches = _impl.search_chats(query=query, limit=limit)
    if response_format == "concise":
        matches = [
            {"chat_id": m.get("chat_id") or m.get("id"),
             "title": m.get("title"),
             "snippet": (m.get("snippet") or "")[:200]}
            for m in matches
        ]
    return {"matches": matches, "_meta": {"response_format": response_format, "count": len(matches)}}


def search_semantic(query: str, top_k: int = 8) -> dict:
    """Semantic search across saved chats using local embeddings. Each chat
    is indexed by its flattened text (user + assistant content). Falls back to
    substring search if the embedding model is unavailable.

    Returns {results, _meta:{search_method}} so the agent can tell whether
    it got actual semantic ranking or a substring fallback.
    """
    from pathlib import Path
    from src import chats as _chats
    from src import embeddings

    items = []
    metas: dict[str, dict] = {}
    for p in sorted(_chats.CHATS_DIR.glob("*.json")):
        try:
            import json
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        chunks = []
        for m in data.get("messages", []):
            if m.get("role") == "user":
                chunks.append(m.get("text", ""))
            else:
                chunks.append(_chats._assistant_text(m))
        text = "\n".join(c for c in chunks if c).strip()
        if not text:
            continue
        # Truncate per-chat to keep encoding fast; long chats encoded as the
        # head, where the topic usually lives.
        items.append({
            "key": data["id"],
            "text": text[:4000],
            "meta": {
                "id": data["id"],
                "title": data.get("title"),
                "started_at": data.get("started_at"),
                "message_count": len(data.get("messages", [])),
            },
        })
        metas[data["id"]] = items[-1]["meta"]

    if not items:
        return {
            "results": [],
            "_meta": {"search_method": "semantic", "empty_reason": "no_chats"},
        }

    embeddings.upsert(scope="chats", items=items)
    embeddings.purge(scope="chats", keep_keys=set(metas.keys()))

    hits = embeddings.query(scope="chats", text=query, top_k=top_k)
    if not hits:
        results = _impl.search_chats(query=query, limit=top_k)
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
