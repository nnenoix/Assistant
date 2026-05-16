"""Local semantic embeddings for notes and chats.

Uses sentence-transformers (CPU-only PyTorch). Downloads a small multilingual
model (~120 MB) on first use into the HuggingFace cache. Caches per-document
embeddings in `.data/embeddings.db` (SQLite) so we only re-encode when content
changes. Falls back to substring search if the model or torch fails to load.

Model: paraphrase-multilingual-MiniLM-L12-v2 — 384-dim vectors, supports 50+
languages including Russian. Loaded once per process, kept in memory.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from src.config import DATA_DIR


EMBEDDINGS_DB = DATA_DIR / "embeddings.db"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_model = None
_model_error: str | None = None


def _get_model():
    """Lazy-load the model. Returns None on failure (caller falls back)."""
    global _model, _model_error
    if _model is not None or _model_error is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    except Exception as e:
        _model_error = f"{type(e).__name__}: {e}"
        _model = None
    return _model


def model_status() -> dict:
    """For debugging — is the model loaded? What error if not?"""
    m = _get_model()
    return {
        "loaded": m is not None,
        "model_name": MODEL_NAME,
        "error": _model_error,
    }


def _conn() -> sqlite3.Connection:
    EMBEDDINGS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(EMBEDDINGS_DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            text TEXT,
            meta TEXT,
            PRIMARY KEY (scope, key)
        )
    """)
    return c


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _to_blob(vec) -> bytes:
    import numpy as np
    return np.asarray(vec, dtype="float32").tobytes()


def _from_blob(blob: bytes):
    import numpy as np
    return np.frombuffer(blob, dtype="float32")


def upsert(scope: str, items: Iterable[dict]) -> int:
    """Encode and store (or refresh) embeddings for a batch of items.

    Each item is `{"key": str, "text": str, "meta": dict?}`. Re-encoding is
    skipped if the same key already has the same content_hash. Returns the
    number of items actually encoded (new or changed).
    """
    model = _get_model()
    if model is None:
        return 0

    items = list(items)
    if not items:
        return 0

    with _conn() as c:
        existing = dict(
            c.execute(
                f"SELECT key, content_hash FROM embeddings WHERE scope=?",
                (scope,),
            ).fetchall()
        )

    to_encode = []
    for it in items:
        h = _hash(it["text"])
        if existing.get(it["key"]) == h:
            continue
        to_encode.append((it, h))

    if not to_encode:
        return 0

    texts = [it["text"] for it, _ in to_encode]
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO embeddings (scope, key, content_hash, vector, text, meta) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    scope,
                    it["key"],
                    h,
                    _to_blob(vec),
                    it["text"],
                    json.dumps(it.get("meta") or {}, ensure_ascii=False),
                )
                for (it, h), vec in zip(to_encode, vectors)
            ],
        )
    return len(to_encode)


def query(scope: str, text: str, top_k: int = 10) -> list[dict]:
    """Find top-k most similar items in a scope. Returns list of
    {key, score, text, meta}. Empty if model is unavailable or scope is empty.
    """
    model = _get_model()
    if model is None:
        return []
    import numpy as np

    q_vec = model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
    q_arr = np.asarray(q_vec, dtype="float32")

    with _conn() as c:
        rows = c.execute(
            "SELECT key, vector, text, meta FROM embeddings WHERE scope=?",
            (scope,),
        ).fetchall()

    if not rows:
        return []

    scored = []
    for key, blob, txt, meta in rows:
        v = _from_blob(blob)
        # both vectors are L2-normalized → cosine == dot product
        score = float(q_arr @ v)
        scored.append({
            "key": key,
            "score": score,
            "text": txt,
            "meta": json.loads(meta) if meta else {},
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[: max(1, top_k)]


def purge(scope: str, keep_keys: set[str] | None = None) -> int:
    """Remove cached vectors for keys no longer present. Returns count purged."""
    with _conn() as c:
        if keep_keys is None:
            cur = c.execute("DELETE FROM embeddings WHERE scope=?", (scope,))
        else:
            keys = list(keep_keys)
            if not keys:
                cur = c.execute("DELETE FROM embeddings WHERE scope=?", (scope,))
            else:
                placeholders = ",".join("?" * len(keys))
                cur = c.execute(
                    f"DELETE FROM embeddings WHERE scope=? AND key NOT IN ({placeholders})",
                    (scope, *keys),
                )
        return cur.rowcount or 0
