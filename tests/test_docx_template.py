"""Tests for src/tools/_docx_template.py.

Strategy: build a tiny .docx fixture in-test via python-docx (placeholders
in plain paragraphs + table cells, split-run placeholder for the
hard-case test), render it, then re-read the output to confirm
substitutions landed and missing vars were preserved + reported.

Skips entirely if python-docx isn't installed in the test environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

docx = pytest.importorskip("docx")  # python-docx; skip whole module if missing

from src.tools import _docx_template as dt


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def simple_template(tmp_path):
    """Single paragraph + one-row table, three placeholders."""
    doc = docx.Document()
    doc.add_paragraph("Hello {name}, your balance is {balance}.")
    t = doc.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Week"
    t.rows[0].cells[1].text = "{week_num}"
    path = tmp_path / "template.docx"
    doc.save(str(path))
    return path


@pytest.fixture
def split_run_template(tmp_path):
    """Force a placeholder to span multiple runs — the hard case where
    a naïve text-level `str.replace` would miss it because each run's
    .text contains only a fragment."""
    doc = docx.Document()
    para = doc.add_paragraph()
    para.add_run("Hello {")        # first run: "Hello {"
    para.add_run("name").bold = True  # second run: "name" (bold)
    para.add_run("}!")             # third run: "}!"
    path = tmp_path / "split.docx"
    doc.save(str(path))
    return path


def _read_text(docx_path):
    """Concat every paragraph + table-cell text from a saved .docx."""
    d = docx.Document(str(docx_path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    parts.append(p.text)
    return "\n".join(parts)


# ============================================================
# list_placeholders
# ============================================================

def test_list_placeholders_finds_paragraphs_and_table_cells(simple_template):
    out = dt.list_placeholders(str(simple_template))
    assert out["ok"] is True
    assert sorted(out["data"]["placeholders"]) == ["balance", "name", "week_num"]
    assert out["data"]["paragraph_count"] >= 1
    assert out["data"]["table_count"] == 1


def test_list_placeholders_returns_empty_for_no_placeholders(tmp_path):
    doc = docx.Document()
    doc.add_paragraph("Nothing dynamic here.")
    p = tmp_path / "static.docx"
    doc.save(str(p))
    out = dt.list_placeholders(str(p))
    assert out["ok"] is True
    assert out["data"]["placeholders"] == []


def test_list_placeholders_rejects_missing_file(tmp_path):
    out = dt.list_placeholders(str(tmp_path / "does-not-exist.docx"))
    assert out["ok"] is False
    assert out["error_kind"] == "not_found"


def test_list_placeholders_rejects_non_docx(tmp_path):
    p = tmp_path / "not-docx.txt"
    p.write_text("hello", encoding="utf-8")
    out = dt.list_placeholders(str(p))
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


# ============================================================
# render
# ============================================================

def test_render_substitutes_all_placeholders(simple_template, tmp_path):
    output = tmp_path / "rendered.docx"
    out = dt.render(str(simple_template), str(output),
                    {"name": "Alice", "balance": "100", "week_num": "5"})
    assert out["ok"] is True
    assert out["data"]["replacements_made"] == 3
    assert out["data"]["missing_vars"] == []
    text = _read_text(output)
    assert "Hello Alice" in text
    assert "balance is 100" in text
    assert "5" in text  # table cell value
    # No placeholder braces left in output
    assert "{name}" not in text
    assert "{week_num}" not in text


def test_render_leaves_missing_placeholders_in_place(simple_template, tmp_path):
    output = tmp_path / "incomplete.docx"
    out = dt.render(str(simple_template), str(output),
                    {"name": "Bob"})  # balance + week_num missing
    assert out["ok"] is True
    assert sorted(out["data"]["missing_vars"]) == ["balance", "week_num"]
    text = _read_text(output)
    assert "Hello Bob" in text  # supplied key substituted
    assert "{balance}" in text  # missing key preserved
    assert "{week_num}" in text


def test_render_handles_split_run_placeholder(split_run_template, tmp_path):
    """A placeholder split across runs — `{`, `name`, `}!` — must still
    be substituted. Naïve per-run str.replace would miss this."""
    output = tmp_path / "split-out.docx"
    out = dt.render(str(split_run_template), str(output),
                    {"name": "Carol"})
    assert out["data"]["replacements_made"] == 1
    text = _read_text(output)
    assert "Hello Carol!" in text


def test_render_ignores_extra_keys(simple_template, tmp_path):
    """`data` may carry keys that aren't in the template — silently
    ignored (caller might pass a kitchen-sink object)."""
    output = tmp_path / "extra.docx"
    out = dt.render(str(simple_template), str(output),
                    {"name": "A", "balance": "1", "week_num": "1",
                     "extra_unused": "noise"})
    assert out["ok"] is True
    assert out["data"]["replacements_made"] == 3
    assert out["data"]["missing_vars"] == []


def test_render_creates_parent_dir(simple_template, tmp_path):
    """Output goes through a non-existent subdir — render should mkdir."""
    nested = tmp_path / "deep" / "nested" / "report.docx"
    out = dt.render(str(simple_template), str(nested),
                    {"name": "X", "balance": "1", "week_num": "1"})
    assert out["ok"] is True
    assert nested.exists()


def test_render_rejects_missing_template(tmp_path):
    out = dt.render(str(tmp_path / "no.docx"), str(tmp_path / "o.docx"),
                    {"name": "X"})
    assert out["ok"] is False
    assert out["error_kind"] == "not_found"


def test_render_rejects_non_dict_data(simple_template, tmp_path):
    out = dt.render(str(simple_template), str(tmp_path / "o.docx"),
                    "not a dict")  # type: ignore[arg-type]
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


# ============================================================
# Real TrueStats template (if shipped with the repo)
# ============================================================

def test_truestats_template_is_parseable():
    """Smoke-test against the actual TrueStats weekly-report template
    if it's present in the repo. Just verifies list_placeholders runs
    cleanly — we don't know the exact placeholder set without opening
    the file, but a successful list call means python-docx accepts it
    and the regex doesn't choke on Cyrillic text."""
    candidates = list(Path(".").glob("TrueStats*.docx"))
    if not candidates:
        pytest.skip("TrueStats template not in repo root")
    out = dt.list_placeholders(str(candidates[0]))
    assert out["ok"] is True
    assert isinstance(out["data"]["placeholders"], list)


# ============================================================
# registry integration
# ============================================================

def test_tools_registered():
    """Both tools should appear in registry.TOOLS by name."""
    from src.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "docx_template_list_placeholders" in names
    assert "docx_template_render" in names
