"""Phase 15 production hardening: edge cases that could silently break.

Verifies clean error messages, no silent failures, graceful degradation on
the most likely production breakage paths.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.tools import _claude_query, file_analyze, file_extract


# ========== empty / whitespace inputs ==========

def test_analyze_rejects_empty_focus(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("real content", encoding="utf-8")
    with pytest.raises(ValueError, match="focus is required"):
        file_analyze.analyze(str(p), focus="")
    with pytest.raises(ValueError, match="focus is required"):
        file_analyze.analyze(str(p), focus="   \n\t  ")


def test_analyze_rejects_empty_extracted_text(tmp_path):
    """File exists but contains only whitespace — should error clearly."""
    p = tmp_path / "blank.txt"
    p.write_text("\n   \n  \t\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        file_analyze.analyze(str(p), focus="anything")


def test_analyze_rejects_missing_file():
    with pytest.raises(FileNotFoundError):
        file_analyze.analyze("/nonexistent/path/x.txt", focus="x")


def test_extract_rejects_empty_path():
    with pytest.raises(ValueError, match="non-empty"):
        file_extract.extract_text("")
    with pytest.raises(ValueError, match="non-empty"):
        file_extract.extract_text("   ")
    with pytest.raises(ValueError, match="non-empty"):
        file_extract.extract_text(None)  # type: ignore[arg-type]


# ========== unsupported / invalid inputs ==========

def test_extract_rejects_unsupported_extension(tmp_path):
    p = tmp_path / "weird.unknownext"
    p.write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        file_extract.extract_text(str(p))


def test_extract_rejects_non_google_url():
    with pytest.raises(ValueError, match="not recognized as Google"):
        file_extract.extract_text("https://random-website.com/data.pdf")


def test_extract_rejects_http_with_no_domain():
    with pytest.raises(ValueError):
        file_extract.extract_text("http://")


# ========== max_chars semantics ==========

def test_max_chars_truncates_and_flags(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("a" * 10_000, encoding="utf-8")
    r = file_extract.extract_text(str(p), max_chars=500)
    assert r["chars"] == 500
    assert r["truncated"] is True


def test_max_chars_does_not_flag_when_under(tmp_path):
    p = tmp_path / "small.txt"
    p.write_text("a" * 100, encoding="utf-8")
    r = file_extract.extract_text(str(p), max_chars=500)
    assert r["truncated"] is False


# ========== ensemble: error isolation ==========

def test_ensemble_pass_a_fails_judge_still_completes(monkeypatch):
    async def _call(model, system_prompt, user_message):
        if model == file_analyze.MODEL_FAST:
            raise RuntimeError("network blip A")
        if "синтез" in system_prompt.lower() or "финальную" in system_prompt.lower():
            return "FINAL synthesis"
        return "B output"

    monkeypatch.setattr(_claude_query, "call", AsyncMock(side_effect=_call))
    r = asyncio.run(file_analyze.ensemble("text content here", "test focus"))
    assert r["_meta"]["pass_a_failed"] is True
    assert r["_meta"]["pass_b_failed"] is False
    assert "Pass A failed" in r["pass_a"]
    assert r["synthesis"] == "FINAL synthesis"


def test_ensemble_pass_b_fails_judge_still_completes(monkeypatch):
    async def _call(model, system_prompt, user_message):
        if model == file_analyze.MODEL_DEEP and "синтез" not in system_prompt.lower() and "финальную" not in system_prompt.lower():
            raise RuntimeError("network blip B")
        if "синтез" in system_prompt.lower() or "финальную" in system_prompt.lower():
            return "FINAL"
        return "A output"

    monkeypatch.setattr(_claude_query, "call", AsyncMock(side_effect=_call))
    r = asyncio.run(file_analyze.ensemble("text", "focus"))
    assert r["_meta"]["pass_a_failed"] is False
    assert r["_meta"]["pass_b_failed"] is True
    assert "Pass B failed" in r["pass_b"]


def test_ensemble_judge_failure_raises_clean(monkeypatch):
    """If A+B succeed but judge fails — raise visible error, don't silently degrade."""
    async def _call(model, system_prompt, user_message):
        if "синтез" in system_prompt.lower() or "финальную" in system_prompt.lower():
            raise RuntimeError("judge unavailable")
        return "ok"
    monkeypatch.setattr(_claude_query, "call", AsyncMock(side_effect=_call))
    with pytest.raises(RuntimeError, match="judge unavailable"):
        asyncio.run(file_analyze.ensemble("text", "focus"))


# ========== save_as sanitization ==========

def test_save_as_strips_dangerous_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)

    async def _call(*args, **kwargs):
        return "synthesis text"
    monkeypatch.setattr(_claude_query, "call", AsyncMock(side_effect=_call))
    monkeypatch.setattr("src.tools.notes.add", lambda text, tag=None: {"id": 1})

    src = tmp_path / "src.txt"
    src.write_text("content", encoding="utf-8")
    r = file_analyze.analyze(
        str(src),
        focus="x",
        save_as="../../etc/passwd:DROP TABLE",
    )
    # No path traversal, no special chars in name
    assert "/" not in r["save_as"]
    assert "\\" not in r["save_as"]
    assert ":" not in r["save_as"]
    # File saved INSIDE ANALYSES_DIR
    assert str(tmp_path) in r["saved_to"]


def test_save_as_caps_length(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    monkeypatch.setattr(_claude_query, "call", AsyncMock(return_value="text"))
    monkeypatch.setattr("src.tools.notes.add", lambda text, tag=None: {"id": 1})

    src = tmp_path / "x.txt"
    src.write_text("c", encoding="utf-8")
    r = file_analyze.analyze(str(src), focus="x", save_as="a" * 200)
    assert len(r["save_as"]) <= 80


def test_save_as_empty_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    monkeypatch.setattr(_claude_query, "call", AsyncMock(return_value="text"))
    monkeypatch.setattr("src.tools.notes.add", lambda text, tag=None: {"id": 1})

    src = tmp_path / "consult.txt"
    src.write_text("c", encoding="utf-8")
    r = file_analyze.analyze(str(src), focus="x", save_as="!!!@@@")
    # All special chars stripped → fallback to 'analysis'
    assert r["save_as"] in ("analysis", "consult")  # depends on sanitize


# ========== notes.add failure should not lose synthesis ==========

def test_analyze_survives_notes_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    monkeypatch.setattr(_claude_query, "call", AsyncMock(return_value="synthesis"))

    def broken_notes_add(text, tag=None):
        raise RuntimeError("notes.json corrupted")
    monkeypatch.setattr("src.tools.notes.add", broken_notes_add)

    src = tmp_path / "x.txt"
    src.write_text("content", encoding="utf-8")
    # Graceful degradation: notes.add failure does NOT lose synthesis
    r = file_analyze.analyze(str(src), focus="x", save_as="test_notes_fail")
    assert r["synthesis"] == "synthesis"
    assert r["notes_id"] is None
    assert "notes_add_failed" in r["_meta"]
    assert "notes.json corrupted" in r["_meta"]["notes_add_failed"]

    # The .md file should ALREADY exist on disk — saved before notes.add
    md = tmp_path / "test_notes_fail.md"
    assert md.exists(), ".md should be saved before notes.add fails"
    assert "synthesis" in md.read_text(encoding="utf-8")


# ========== analyses_search robustness ==========

def test_analyses_search_empty_query():
    """Empty query shouldn't crash — should return empty results."""
    r = file_analyze.search_analyses("")
    assert "results" in r


def test_analyses_read_missing_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Analysis not found"):
        file_analyze.read_analysis("does_not_exist")


def test_analyses_list_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    r = file_analyze.list_analyses()
    assert r["analyses"] == []
    assert r["_meta"]["count"] == 0


def test_analyses_list_handles_corrupted_md(tmp_path, monkeypatch):
    """An .md file with no front-matter should not crash list_analyses."""
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    (tmp_path / "corrupted.md").write_text("not a real analysis", encoding="utf-8")
    (tmp_path / "good.md").write_text(
        file_analyze._front_matter({"source": "x", "focus": "y"}) + "body",
        encoding="utf-8",
    )
    r = file_analyze.list_analyses()
    names = [a["name"] for a in r["analyses"]]
    assert "corrupted" in names
    assert "good" in names


# ========== security hardening (post-/security-review) ==========

def test_apply_max_chars_rejects_negative():
    """Negative max_chars caused silent trailing trim (text[:-N]) — reject it."""
    from src.tools import file_extract as fe
    with pytest.raises(ValueError, match="non-negative"):
        fe._apply_max_chars("hello world", -5)


def test_claude_query_rejects_unknown_model():
    """Model whitelist prevents arbitrary strings reaching subprocess args."""
    import asyncio as _aio
    from src.tools import _claude_query
    with pytest.raises(ValueError, match="not in allowlist"):
        _aio.run(_claude_query.call(
            model="evil-model; rm -rf /",
            system_prompt="x",
            user_message="y",
        ))


def test_claude_query_allows_known_models():
    """All 6 documented model aliases pass the whitelist check."""
    from src.tools import _claude_query
    for m in ("haiku", "sonnet", "opus",
              "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"):
        assert m in _claude_query.ALLOWED_MODELS


def test_notes_add_thread_safe(tmp_path, monkeypatch):
    """Concurrent notes.add() must not lose entries or collide on id."""
    from src.tools import notes
    import threading
    tmp_notes = tmp_path / "notes.json"
    monkeypatch.setattr(notes, "NOTES_FILE", tmp_notes)

    results: list[dict] = []
    lock = threading.Lock()

    def worker(i):
        entry = notes.add(f"note_{i}", tag=f"t_{i}")
        with lock:
            results.append(entry)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 30
    # All ids must be unique
    ids = [r["id"] for r in results]
    assert len(set(ids)) == 30, f"Duplicate IDs: {sorted(ids)}"
    # All 30 entries persisted
    stored = notes.list_notes(limit=100)
    assert len(stored) == 30
