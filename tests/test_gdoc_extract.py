"""Unit tests for _gdoc_extract and _gsheet_extract (Phase 15D)."""
from unittest.mock import patch

import pytest

from src.tools import _gdoc_extract, _gsheet_extract, file_extract


# ============ gdoc ============

def test_gdoc_extract_uses_docs_api(monkeypatch):
    fake_doc = {
        "title": "Сводка консультации",
        "body_text": "Полное содержание документа на русском.",
        "headings": [{"text": "Введение", "style": "HEADING_1"}],
        "_meta": {"char_count_total": 42, "body_truncated": False},
    }
    with patch("src.tools.docs.read", return_value=fake_doc):
        r = _gdoc_extract.extract("DOC_ID_123", source_url="https://docs.google.com/document/d/DOC_ID_123/edit")

    assert r["file_kind"] == "gdoc"
    assert r["text"] == "Полное содержание документа на русском."
    assert r["chars"] == len(r["text"])
    assert r["truncated"] is False
    assert r["_meta"]["title"] == "Сводка консультации"
    assert r["_meta"]["headings"][0]["text"] == "Введение"
    assert r["_meta"]["extraction_method"] == "docs_api"


def test_gdoc_extract_respects_max_chars(monkeypatch):
    fake_doc = {
        "title": "x",
        "body_text": "a" * 500,
        "headings": [],
        "_meta": {"char_count_total": 500},
    }
    with patch("src.tools.docs.read", return_value=fake_doc):
        r = _gdoc_extract.extract("DOC_ID", max_chars=100)
    assert r["chars"] == 100
    assert r["truncated"] is True


def test_gdoc_extract_propagates_docs_body_truncated(monkeypatch):
    """If docs.read flagged body_truncated, gdoc_extract carries it forward."""
    fake_doc = {
        "title": "Big doc",
        "body_text": "short",
        "headings": [],
        "_meta": {"char_count_total": 100_000, "body_truncated": True},
    }
    with patch("src.tools.docs.read", return_value=fake_doc):
        r = _gdoc_extract.extract("DOC_ID")
    assert r["truncated"] is True


# ============ gsheet ============

def test_gsheet_extract_uses_summarize(monkeypatch):
    fake_summary = {
        "spreadsheet_id": "SHEET_ID",
        "title": "Финансовая отчётность Q1",
        "sheets": [
            {
                "name": "Год факт",
                "rows_total": 100,
                "cols_total": 12,
                "headers": ["Метрика", "Янв", "Фев", "Мар"],
                "sample_rows": [
                    ["Выручка", 100000, 105000, 110000],
                    ["Прибыль", 20000, 21000, 23000],
                ],
            }
        ],
    }
    with patch("src.tools.sheets.summarize", return_value=fake_summary):
        r = _gsheet_extract.extract("SHEET_ID", source_url="https://docs.google.com/spreadsheets/d/SHEET_ID/edit")

    assert r["file_kind"] == "gsheet"
    assert "Финансовая отчётность Q1" in r["text"]
    assert "Год факт" in r["text"]
    assert "Метрика | Янв | Фев | Мар" in r["text"]
    assert "Выручка | 100000 | 105000 | 110000" in r["text"]
    assert r["_meta"]["title"] == "Финансовая отчётность Q1"
    assert r["_meta"]["tabs_count"] == 1


def test_gsheet_extract_truncates(monkeypatch):
    fake = {
        "spreadsheet_id": "SID",
        "title": "x",
        "sheets": [{
            "name": "S",
            "rows_total": 1,
            "cols_total": 1,
            "headers": ["a"],
            "sample_rows": [["b" * 1000]],
        }],
    }
    with patch("src.tools.sheets.summarize", return_value=fake):
        r = _gsheet_extract.extract("SID", max_chars=50)
    assert r["chars"] == 50
    assert r["truncated"] is True


# ============ file_extract dispatcher routes URLs correctly ============

def test_file_extract_routes_gdoc_url():
    url = "https://docs.google.com/document/d/1abcDEFghi567890_-XYZ12345/edit?usp=sharing"
    fake = {
        "title": "Test",
        "body_text": "content",
        "headings": [],
        "_meta": {"char_count_total": 7},
    }
    with patch("src.tools.docs.read", return_value=fake):
        r = file_extract.extract_text(url)
    assert r["file_kind"] == "gdoc"
    assert r["text"] == "content"


def test_file_extract_routes_gsheet_url():
    url = "https://docs.google.com/spreadsheets/d/1abcDEFghi567890_-XYZ12345/edit"
    fake = {
        "spreadsheet_id": "1abcDEFghi567890_-XYZ12345",
        "title": "test",
        "sheets": [],
    }
    with patch("src.tools.sheets.summarize", return_value=fake):
        r = file_extract.extract_text(url)
    assert r["file_kind"] == "gsheet"
