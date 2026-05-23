"""Tests for the local knowledge-base tools.

`src/embeddings.py` does the heavy lifting. These tests treat the
embeddings module as the contract — we monkeypatch DB_PATH to a tmp
file so each test gets its own clean store, and we exercise the
public knowledge.save / search / list_sources / delete surface.

The embedding model itself isn't loaded in tests (sentence-transformers
is heavy + may not be installed) — we patch `embeddings._get_model`
to return None, which exercises the substring-fallback path the
real module ships for the same scenario.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_kb(tmp_path, monkeypatch):
    """Redirect embeddings.sqlite to a tmp file + force the substring
    fallback by stubbing out _get_model."""
    from src import embeddings
    monkeypatch.setattr(embeddings, "EMBEDDINGS_DB", tmp_path / "kb.sqlite")
    # No real model loading — substring fallback covers everything
    # the suite needs to assert.
    monkeypatch.setattr(embeddings, "_get_model", lambda: _FakeModel())
    return tmp_path


class _FakeModel:
    """Deterministic 4-dim "embedding" — just hashes characters into
    a fixed-size vector. Good enough that cosine similarity orders
    text-substring matches sensibly."""
    def encode(self, texts, normalize_embeddings=False, show_progress_bar=False):
        import numpy as np
        out = []
        for t in texts:
            t = t.lower()
            vec = np.array([
                sum(ord(c) for c in t if c.isalnum()) % 100,
                len(t) % 100,
                t.count("a") + t.count("а"),
                t.count("e") + t.count("е"),
            ], dtype="float32")
            if normalize_embeddings:
                n = np.linalg.norm(vec)
                if n > 0:
                    vec = vec / n
            out.append(vec)
        return np.array(out, dtype="float32")


# ============================================================
# save
# ============================================================

def test_save_creates_entry(fresh_kb):
    from src.tools import knowledge
    out = knowledge.save(
        text="Wildberries промо: оплата за клик от 2000₽/неделю",
        source="https://seller.wildberries.ru/promotion",
        title="WB Продвижение",
        tags=["wildberries", "promotion"],
    )
    assert out["ok"] is True
    assert out["key"]
    assert out["encoded"] is True


def test_save_is_idempotent_on_same_source_and_text(fresh_kb):
    from src.tools import knowledge
    first = knowledge.save("hello", source="https://x.com")
    second = knowledge.save("hello", source="https://x.com")
    assert first["key"] == second["key"]
    # First call encoded; second was a no-op
    assert first["encoded"] is True
    assert second["encoded"] is False


def test_save_rejects_empty_text(fresh_kb):
    from src.tools import knowledge
    out = knowledge.save(text="   ", source="https://x.com")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


def test_save_rejects_empty_source(fresh_kb):
    from src.tools import knowledge
    out = knowledge.save(text="hi", source="")
    assert out["ok"] is False


def test_save_rejects_too_many_tags(fresh_kb):
    from src.tools import knowledge
    out = knowledge.save(text="hi", source="x", tags=[f"t{i}" for i in range(25)])
    assert out["ok"] is False
    assert "too many tags" in out["error"]


def test_save_rejects_oversized_text(fresh_kb):
    from src.tools import knowledge
    out = knowledge.save(text="x" * 300_000, source="x")
    assert out["ok"] is False
    assert "exceeds" in out["error"]


# ============================================================
# search
# ============================================================

def test_search_finds_saved_entry(fresh_kb):
    from src.tools import knowledge
    knowledge.save(
        text="Ozon продвижение: минимальная ставка 23 процента по всем категориям",
        source="https://docs.ozon.ru/promo",
        title="Ozon продвижение",
        tags=["ozon"],
    )
    out = knowledge.search(query="ozon продвижение")
    assert out["ok"] is True
    assert len(out["results"]) >= 1
    hit = out["results"][0]
    assert hit["title"] == "Ozon продвижение"
    assert hit["source"] == "https://docs.ozon.ru/promo"
    assert "23 процента" in hit["snippet"]
    assert "ozon" in hit["tags"]


def test_search_tag_filter_intersection(fresh_kb):
    from src.tools import knowledge
    knowledge.save(text="WB info one", source="s1", tags=["wb", "promo"])
    knowledge.save(text="WB info two", source="s2", tags=["wb"])
    knowledge.save(text="Ozon info",   source="s3", tags=["ozon", "promo"])

    # Only entries with ALL listed tags pass
    out = knowledge.search(query="info", tag_filter=["wb", "promo"])
    assert len(out["results"]) == 1
    assert out["results"][0]["source"] == "s1"


def test_search_rejects_empty_query(fresh_kb):
    from src.tools import knowledge
    out = knowledge.search(query="")
    assert out["ok"] is False


def test_search_top_k_caps(fresh_kb):
    from src.tools import knowledge
    out = knowledge.search(query="x", top_k=200)
    assert out["ok"] is False
    assert "1..100" in out["error"]


def test_search_on_empty_kb_returns_no_results(fresh_kb):
    from src.tools import knowledge
    out = knowledge.search(query="anything")
    assert out["ok"] is True
    assert out["results"] == []


# ============================================================
# list_sources
# ============================================================

def test_list_sources_groups_by_source(fresh_kb):
    from src.tools import knowledge
    knowledge.save(text="chunk 1", source="https://a.com", title="Page A")
    knowledge.save(text="chunk 2", source="https://a.com", title="Page A")
    knowledge.save(text="chunk 3", source="https://b.com", title="Page B")

    out = knowledge.list_sources()
    assert out["ok"] is True
    # Two distinct sources
    sources = {s["source"] for s in out["sources"]}
    assert sources == {"https://a.com", "https://b.com"}
    # a.com has count 2
    a = next(s for s in out["sources"] if s["source"] == "https://a.com")
    assert a["count"] == 2


def test_list_sources_newest_first(fresh_kb):
    import time as _t
    from src.tools import knowledge
    knowledge.save(text="old", source="s_old")
    _t.sleep(0.02)
    knowledge.save(text="new", source="s_new")
    out = knowledge.list_sources()
    assert out["sources"][0]["source"] == "s_new"


# ============================================================
# delete
# ============================================================

def test_delete_removes_entry(fresh_kb):
    from src.tools import knowledge
    saved = knowledge.save(text="goodbye", source="s")
    out = knowledge.delete(saved["key"])
    assert out["ok"] is True
    assert out["removed"] == 1
    # Now invisible
    search = knowledge.search(query="goodbye")
    assert search["results"] == []


def test_delete_missing_key_is_idempotent(fresh_kb):
    from src.tools import knowledge
    out = knowledge.delete(key="does-not-exist")
    assert out["ok"] is True
    assert out["removed"] == 0


def test_delete_rejects_empty_key(fresh_kb):
    from src.tools import knowledge
    out = knowledge.delete(key="")
    assert out["ok"] is False


# ============================================================
# Registry wiring
# ============================================================

def test_knowledge_tools_are_registered():
    from src.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    for n in ("knowledge_save", "knowledge_search",
              "knowledge_list_sources", "knowledge_delete"):
        assert n in names, f"missing: {n}"


def test_knowledge_tools_have_correct_policy_ops():
    from src.tools.registry import TOOLS
    by = {t["name"]: t for t in TOOLS}
    # Writes
    assert by["knowledge_save"]["policy_op"] == "notes.write"
    assert by["knowledge_delete"]["policy_op"] == "notes.write"
    # Reads
    assert by["knowledge_search"]["policy_op"] == "notes.read"
    assert by["knowledge_list_sources"]["policy_op"] == "notes.read"
