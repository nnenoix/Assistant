"""Local knowledge base tools — read once, recall forever.

Wraps `src/embeddings.py` (sentence-transformers + sqlite) into four
agent-facing tools so Claude can stash facts mid-conversation and find
them in any future chat:

    knowledge_save(text, source, …)    → write
    knowledge_search(query, …)         → semantic recall
    knowledge_list_sources(…)          → "what do I already know about?"
    knowledge_delete(key, …)           → forget a specific entry

Storage lives at `.data/embeddings.sqlite` (already in `.gitignore`).
Records are scoped — default scope is "knowledge" so we don't collide
with the chat-search scope.

Why this matters: without persistent recall, every chat starts blind.
The agent had to re-fetch / re-parse the same web pages, the same
Drive folders. With save/search, a once-fetched fact survives
indefinitely and can be cited by source URL on later reference.

Design choices:
- Plain text body (not summaries) — the agent decides what to extract.
  We store what it gives us, search returns top-k semantically.
- Tags are free-form strings; passed as a list, stored as JSON in
  `meta.tags`. Search can filter by exact-match tag intersection.
- `source` is the canonical identifier (URL / file path / Drive ID).
  Same source + same text content → idempotent (embeddings.upsert
  short-circuits on identical content_hash).
- Keys are derived as sha256(source||text)[:16] so:
    - saving the same fact twice doesn't duplicate
    - explicit `key=` lets callers update an entry
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from src import embeddings

_DEFAULT_SCOPE = "knowledge"
_MAX_TEXT_BYTES = 200_000  # 200KB per entry — generous for a single page
_MAX_TAGS = 20


def _derive_key(source: str, text: str) -> str:
    return hashlib.sha256(
        f"{source}||{text}".encode("utf-8"),
    ).hexdigest()[:16]


def save(
    text: str,
    source: str,
    title: str | None = None,
    tags: list[str] | None = None,
    scope: str = _DEFAULT_SCOPE,
    key: str | None = None,
) -> dict:
    """Save a chunk of text to the local knowledge base with metadata.

    `source` is the canonical pointer back to where this came from —
    a URL, a Drive file ID, an absolute file path. `title` is a
    human-readable label shown in search results / list_sources.
    `tags` is a list of free-form labels for later filtering.

    Returns {ok, key, encoded: bool, _meta}. `encoded=False` means the
    same key + same content already existed — nothing was re-encoded
    (embeddings model is the expensive bit). Idempotent."""
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error_kind": "bad_input",
                "error": "text must be a non-empty string"}
    if not isinstance(source, str) or not source.strip():
        return {"ok": False, "error_kind": "bad_input",
                "error": "source must be a non-empty string"}

    text = text.strip()
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        return {"ok": False, "error_kind": "bad_input",
                "error": f"text exceeds {_MAX_TEXT_BYTES} bytes — split it"}

    tags = list(tags or [])
    if len(tags) > _MAX_TAGS:
        return {"ok": False, "error_kind": "bad_input",
                "error": f"too many tags ({len(tags)} > {_MAX_TAGS})"}

    final_key = key or _derive_key(source, text)
    meta = {
        "source": source,
        "title": (title or source)[:200],
        "tags": tags,
        "saved_at": time.time(),
        "scope": scope,
    }
    encoded = embeddings.upsert(scope, [{
        "key": final_key,
        "text": text,
        "meta": meta,
    }])
    return {
        "ok": True,
        "key": final_key,
        "encoded": encoded > 0,
        "_meta": {
            "scope": scope,
            "text_bytes": len(text.encode("utf-8")),
            "title": meta["title"],
        },
    }


def search(
    query: str,
    top_k: int = 8,
    scope: str = _DEFAULT_SCOPE,
    tag_filter: list[str] | None = None,
) -> dict:
    """Semantic search across saved entries. Returns top-k hits sorted
    by similarity. Each hit: {key, score, title, source, snippet, tags}.

    `tag_filter`: if provided, only entries that have ALL of these tags
    pass (intersection, not union). Empty list = no filter.

    Falls back to substring search on `text` when the embedding model
    isn't available (test env, slim builds). `_meta.search_method`
    tells the caller which path was used."""
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error_kind": "bad_input",
                "error": "query must be a non-empty string"}
    if top_k < 1 or top_k > 100:
        return {"ok": False, "error_kind": "bad_input",
                "error": "top_k must be in 1..100"}

    tag_filter = list(tag_filter or [])

    hits = embeddings.query(scope, query, top_k=max(top_k * 3, top_k))
    method = "semantic" if hits else "none"

    if not hits:
        # Substring fallback — scan raw rows by content. Cheap when the
        # KB is small (<1k entries) which is the realistic case for a
        # personal agent.
        rows = _scan_all(scope)
        q_lower = query.lower()
        for r in rows:
            if q_lower in (r.get("text") or "").lower():
                hits.append({"key": r["key"], "score": 0.5,
                             "text": r["text"], "meta": r.get("meta") or {}})
            if len(hits) >= top_k * 3:
                break
        method = "substring" if hits else "none"

    # Apply tag filter, build snippet
    results: list[dict] = []
    for h in hits:
        meta = h.get("meta") or {}
        if tag_filter:
            entry_tags = set(meta.get("tags") or [])
            if not all(t in entry_tags for t in tag_filter):
                continue
        snippet = (h.get("text") or "")[:300]
        if len(h.get("text") or "") > 300:
            snippet += "…"
        results.append({
            "key": h["key"],
            "score": round(h.get("score", 0.0), 4),
            "title": meta.get("title"),
            "source": meta.get("source"),
            "tags": meta.get("tags") or [],
            "snippet": snippet,
        })
        if len(results) >= top_k:
            break

    return {
        "ok": True,
        "results": results,
        "_meta": {
            "scope": scope,
            "search_method": method,
            "tag_filter": tag_filter,
            "count": len(results),
        },
    }


def list_sources(scope: str = _DEFAULT_SCOPE, limit: int = 50) -> dict:
    """List distinct sources saved in `scope`, newest first. Useful for
    "what do I already know about?" and "what URL did I save earlier?"
    queries. Returns {sources: [{source, title, tags, saved_at, count}]}."""
    rows = _scan_all(scope)
    # group by source
    grouped: dict[str, dict] = {}
    for r in rows:
        meta = r.get("meta") or {}
        src = meta.get("source") or r["key"]
        g = grouped.setdefault(src, {
            "source": src,
            "title": meta.get("title"),
            "tags": meta.get("tags") or [],
            "saved_at": meta.get("saved_at"),
            "count": 0,
            "keys": [],
        })
        g["count"] += 1
        g["keys"].append(r["key"])
        # If we see a newer saved_at for the same source, take its title
        if (meta.get("saved_at") or 0) > (g.get("saved_at") or 0):
            g["title"] = meta.get("title")
            g["saved_at"] = meta.get("saved_at")
    sources = sorted(grouped.values(), key=lambda s: s.get("saved_at") or 0, reverse=True)
    return {
        "ok": True,
        "sources": sources[:limit],
        "_meta": {"scope": scope, "total_entries": len(rows),
                  "distinct_sources": len(sources)},
    }


def delete(key: str, scope: str = _DEFAULT_SCOPE) -> dict:
    """Remove a single entry by key. Idempotent — returns ok even if
    the key didn't exist."""
    import sqlite3
    from src.embeddings import EMBEDDINGS_DB as DB_PATH
    if not isinstance(key, str) or not key.strip():
        return {"ok": False, "error_kind": "bad_input",
                "error": "key must be a non-empty string"}
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            cur = c.execute(
                "DELETE FROM embeddings WHERE scope = ? AND key = ?",
                (scope, key),
            )
            removed = cur.rowcount
        return {"ok": True, "removed": removed, "key": key}
    except sqlite3.OperationalError:
        # No DB yet — nothing to remove
        return {"ok": True, "removed": 0, "key": key}


def _scan_all(scope: str) -> list[dict]:
    """Read every row in `scope`, deserializing meta. Used by both
    substring fallback in search() and by list_sources(). Cheap as
    long as the scope stays small (<10k entries)."""
    import sqlite3
    from src.embeddings import EMBEDDINGS_DB as DB_PATH
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            cur = c.execute(
                "SELECT key, text, meta FROM embeddings WHERE scope = ?",
                (scope,),
            )
            out = []
            for k, t, m in cur.fetchall():
                try:
                    meta = json.loads(m) if m else {}
                except Exception:
                    meta = {}
                out.append({"key": k, "text": t, "meta": meta})
            return out
    except sqlite3.OperationalError:
        # `embeddings` table doesn't exist yet — first call ever
        return []
