"""Unit tests for src/tools/file_analyze.py — 3-LLM ensemble + memory layer.

Uses claude_agent_sdk's query() under the hood (CLI subscription auth).
Tests mock src.tools._claude_query.call which wraps query().
"""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.tools import _claude_query, file_analyze


def _mock_call_with_responses(responses: list[str]):
    """Build an AsyncMock that returns the given texts in order on each call to _claude_query.call."""
    queue = list(responses)

    async def _call(model, system_prompt, user_message):
        return queue.pop(0) if queue else "[mock empty]"

    return AsyncMock(side_effect=_call)


# ============== ensemble() ==============

def test_ensemble_three_calls(monkeypatch):
    """3 LLM calls happen: pass A, pass B, judge."""
    mock_call = _mock_call_with_responses([
        "PASS A facts",
        "PASS B interpretation",
        "JUDGE synthesis",
    ])
    monkeypatch.setattr(_claude_query, "call", mock_call)

    result = asyncio.run(file_analyze.ensemble("Sample text content", "extract key points"))

    assert result["pass_a"] == "PASS A facts"
    assert result["pass_b"] == "PASS B interpretation"
    assert result["synthesis"] == "JUDGE synthesis"
    assert mock_call.call_count == 3
    assert result["_meta"]["model_a"] == file_analyze.MODEL_FAST
    assert result["_meta"]["model_b"] == file_analyze.MODEL_DEEP
    assert result["_meta"]["judge"] == file_analyze.MODEL_JUDGE


def test_ensemble_judge_sees_both_outputs_and_original(monkeypatch):
    """Judge user message must include both pass outputs AND original excerpt."""
    mock_call = _mock_call_with_responses(["A_TEXT", "B_TEXT", "FINAL"])
    monkeypatch.setattr(_claude_query, "call", mock_call)

    asyncio.run(file_analyze.ensemble("ORIGINAL_TEXT_HERE", "test focus"))

    # Third call (judge) — check user_message
    judge_call = mock_call.call_args_list[2]
    judge_user_msg = judge_call.kwargs["user_message"]
    assert "A_TEXT" in judge_user_msg
    assert "B_TEXT" in judge_user_msg
    assert "ORIGINAL_TEXT_HERE" in judge_user_msg
    assert "test focus" in judge_user_msg


def test_ensemble_passes_a_and_b_run_in_parallel(monkeypatch):
    """Wall-clock should be ~max(A, B) + judge, not sum."""
    delays = {file_analyze.MODEL_FAST: 0.1, file_analyze.MODEL_DEEP: 0.1}
    judge_done = {"called": False}

    async def _call(model, system_prompt, user_message):
        # First two calls have unique system prompts; judge has a different system prompt
        if "синтез" in system_prompt.lower() or "финальную" in system_prompt.lower():
            await asyncio.sleep(0.05)
            return "JUDGE"
        await asyncio.sleep(delays[model])
        return "A" if model == file_analyze.MODEL_FAST else "B"

    mock_call = AsyncMock(side_effect=_call)
    monkeypatch.setattr(_claude_query, "call", mock_call)

    t0 = time.perf_counter()
    result = asyncio.run(file_analyze.ensemble("text", "focus"))
    elapsed = time.perf_counter() - t0

    # Parallel A+B (max=0.1) + judge (0.05) = ~0.15s. Serial would be 0.25s.
    assert elapsed < 0.22, f"parallelism failed: {elapsed:.2f}s"
    assert result["synthesis"] == "JUDGE"


def test_ensemble_pass_a_fails_but_b_and_judge_complete(monkeypatch):
    """If A throws, B still runs, judge sees error marker for A."""
    async def _call(model, system_prompt, user_message):
        if model == file_analyze.MODEL_FAST:
            raise RuntimeError("simulated A failure")
        if "синтез" in system_prompt.lower() or "финальную" in system_prompt.lower():
            return "JUDGE output"
        return "B output"

    mock_call = AsyncMock(side_effect=_call)
    monkeypatch.setattr(_claude_query, "call", mock_call)

    result = asyncio.run(file_analyze.ensemble("text", "focus"))
    assert result["_meta"]["pass_a_failed"] is True
    assert result["_meta"]["pass_b_failed"] is False
    assert "Pass A failed" in result["pass_a"]
    assert result["pass_b"] == "B output"
    assert result["synthesis"] == "JUDGE output"


def test_ensemble_both_passes_fail_raises(monkeypatch):
    async def _call(**kwargs):
        raise RuntimeError("simulated")
    monkeypatch.setattr(_claude_query, "call", AsyncMock(side_effect=_call))

    with pytest.raises(RuntimeError, match="Both pass"):
        asyncio.run(file_analyze.ensemble("text", "focus"))


def test_ensemble_rejects_empty_text(monkeypatch):
    with pytest.raises(ValueError, match="text is empty"):
        asyncio.run(file_analyze.ensemble("   ", "focus"))


def test_ensemble_rejects_empty_focus(monkeypatch):
    monkeypatch.setattr(_claude_query, "call", _mock_call_with_responses(["x", "y", "z"]))
    with pytest.raises(ValueError, match="focus is empty"):
        asyncio.run(file_analyze.ensemble("some text", "  "))


# ============== analyze() — end-to-end ==============

def test_analyze_writes_md_and_indexes_note(tmp_path, monkeypatch):
    # Redirect analyses dir to tmp
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path / "analyses")
    (tmp_path / "analyses").mkdir()

    # Source file
    src = tmp_path / "transcript.txt"
    src.write_text("Содержание созвона. Клиент жалуется на убытки.", encoding="utf-8")

    # Mock LLM
    monkeypatch.setattr(_claude_query, "call", _mock_call_with_responses([
        "facts: убытки упомянуты",
        "interpretation: клиент в кризисе",
        "## Главное\nКлиент в кризисе, нужна помощь.\n\n## Боли\n- Убытки",
    ]))

    # Track notes.add call
    notes_added = []
    def fake_notes_add(text, tag=None):
        notes_added.append({"text": text, "tag": tag})
        return {"id": 42, "ts": "now", "text": text, "tag": tag}
    monkeypatch.setattr("src.tools.notes.add", fake_notes_add)

    result = file_analyze.analyze(
        str(src), focus="боли клиента", save_as="test_smoke",
    )

    assert result["save_as"] == "test_smoke"
    md_path = tmp_path / "analyses" / "test_smoke.md"
    assert md_path.exists()

    content = md_path.read_text(encoding="utf-8")
    # YAML front matter
    assert content.startswith("---\n")
    assert "source:" in content
    assert "focus: \"боли клиента\"" in content
    # Synthesis body
    assert "## Главное" in content
    assert "Клиент в кризисе" in content
    # Pass A and B preserved
    assert "facts: убытки упомянуты" in content
    assert "interpretation: клиент в кризисе" in content

    # notes.add was called with tag prefix analysis:
    assert len(notes_added) == 1
    assert notes_added[0]["tag"] == "analysis:test_smoke"
    assert "боли клиента" in notes_added[0]["text"]
    assert result["notes_id"] == 42


def test_analyze_rejects_empty_focus(tmp_path):
    src = tmp_path / "x.txt"
    src.write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="focus is required"):
        file_analyze.analyze(str(src), focus="")


def test_analyze_rejects_empty_extracted_text(tmp_path, monkeypatch):
    src = tmp_path / "empty.txt"
    src.write_text("   \n\n   ", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        file_analyze.analyze(str(src), focus="anything")


def test_analyze_auto_save_name(tmp_path, monkeypatch):
    """When save_as=None, auto-generate from source filename + timestamp."""
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path / "analyses")
    (tmp_path / "analyses").mkdir()
    src = tmp_path / "consultation.txt"
    src.write_text("content", encoding="utf-8")

    monkeypatch.setattr(_claude_query, "call", _mock_call_with_responses(["a", "b", "c"]))
    monkeypatch.setattr("src.tools.notes.add", lambda text, tag=None: {"id": 1})

    result = file_analyze.analyze(str(src), focus="x")
    assert result["save_as"].startswith("consultation_")
    # ISO-ish timestamp suffix
    assert "T" in result["save_as"] and "-" in result["save_as"]


# ============== _safe_filename ==============

def test_safe_filename_basic():
    assert file_analyze._safe_filename("hello world") == "hello_world"


def test_safe_filename_preserves_cyrillic():
    assert file_analyze._safe_filename("консультация_2026") == "консультация_2026"


def test_safe_filename_strips_special():
    assert file_analyze._safe_filename("file/path:1234.txt") == "file_path_1234_txt"


def test_safe_filename_empty_fallback():
    assert file_analyze._safe_filename("") == "analysis"
    assert file_analyze._safe_filename("///") == "analysis"


def test_safe_filename_caps_length():
    assert len(file_analyze._safe_filename("a" * 200)) == 80


# ============== front matter ==============

def test_front_matter_roundtrip():
    meta_in = {
        "source": "D:/path/to/file.txt",
        "chars_in": 12345,
        "focus": "test focus with \"quotes\"",
        "truncated": True,
        "score": 3.14,
        "tag": None,
    }
    fm = file_analyze._front_matter(meta_in)
    parsed = file_analyze._parse_front_matter(fm + "\nbody\n")
    assert parsed["source"] == meta_in["source"]
    assert parsed["chars_in"] == 12345
    assert parsed["focus"] == 'test focus with "quotes"'
    assert parsed["truncated"] is True
    assert parsed["score"] == 3.14
    assert parsed["tag"] is None


def test_parse_front_matter_returns_empty_on_no_marker():
    assert file_analyze._parse_front_matter("just body") == {}
    assert file_analyze._parse_front_matter("---\nunclosed\nbody") == {}


def test_front_matter_escapes_newlines_and_returns():
    """A value containing \\n must not break front-matter (single-line per key)."""
    fm = file_analyze._front_matter({
        "focus": "line one\nline two",
        "note": "with\rCR",
    })
    # No raw newline inside the value — each key on its own line
    body = fm.split("---\n")[1]  # between markers
    lines = [l for l in body.split("\n") if l.strip()]
    assert any("focus:" in l and "\\n" in l for l in lines)
    assert any("note:" in l and "\\r" in l for l in lines)


# ============== list_analyses / read_analysis ==============

def test_list_analyses_returns_sorted_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)

    def _write(name: str, body: str = "Hi"):
        path = tmp_path / f"{name}.md"
        path.write_text(
            file_analyze._front_matter({
                "source": f"src_{name}",
                "focus": "test",
                "created_at": "2026-01-01",
            }) + body,
            encoding="utf-8",
        )
        return path

    a = _write("first")
    time.sleep(0.05)
    b = _write("second")
    time.sleep(0.05)
    c = _write("third")

    result = file_analyze.list_analyses()
    names = [a["name"] for a in result["analyses"]]
    assert names == ["third", "second", "first"]
    assert result["_meta"]["count"] == 3


def test_read_analysis_returns_content_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    body = file_analyze._front_matter({
        "source": "x.txt",
        "focus": "y",
        "chars_in": 100,
    }) + "# Hello\nWorld\n"
    (tmp_path / "myanalysis.md").write_text(body, encoding="utf-8")

    r = file_analyze.read_analysis("myanalysis")
    assert r["name"] == "myanalysis"
    assert "# Hello" in r["content"]
    assert r["_meta"]["chars_in"] == 100
    assert r["_meta"]["source"] == "x.txt"


def test_read_analysis_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        file_analyze.read_analysis("nonexistent")


def test_read_analysis_accepts_md_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(file_analyze, "ANALYSES_DIR", tmp_path)
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    r = file_analyze.read_analysis("a.md")
    assert r["name"] == "a"


# ============== search_analyses ==============

def test_search_analyses_filters_by_tag(monkeypatch):
    fake_results = {
        "results": [
            {"tag": "analysis:foo", "score": 0.9, "text": "foo body about клиенте"},
            {"tag": "regular_note", "score": 0.8, "text": "irrelevant"},
            {"tag": "analysis:bar", "score": 0.7, "text": "bar body"},
        ],
        "_meta": {"search_method": "semantic"},
    }
    monkeypatch.setattr("src.tools.notes.search_semantic", lambda q, top_k=8: fake_results)
    r = file_analyze.search_analyses("clients pain", top_k=5)
    names = [h["name"] for h in r["results"]]
    assert names == ["foo", "bar"]
    assert "regular_note" not in [h["tag"] for h in r["results"]]
    assert r["_meta"]["search_method"] == "semantic"
