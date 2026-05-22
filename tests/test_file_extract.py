"""Unit tests for src/tools/file_extract.py — universal dispatcher."""
from pathlib import Path
from unittest.mock import patch

import pytest

from src.tools import file_extract


# ---------- routing ----------

def test_dispatch_txt(tmp_path):
    p = tmp_path / "hello.txt"
    p.write_text("Привет мир\nВторая строка\n", encoding="utf-8")
    r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "text"
    assert "Привет мир" in r["text"]
    assert r["chars"] == len(r["text"])


def test_dispatch_md(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Header\nBody.\n", encoding="utf-8")
    r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "text"
    assert "Header" in r["text"]


def test_dispatch_csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "text"


def test_dispatch_pdf_via_existing_extractor(tmp_path, monkeypatch):
    # Mock the underlying local_fs.extract_pdf_text — we just verify dispatch.
    fake = {"text": "PDF body", "chars": 8, "truncated": False,
            "pages_count": 2, "file_name": "x.pdf"}
    p = tmp_path / "x.pdf"
    p.write_bytes(b"not real pdf bytes")
    with patch("src.tools.local_fs.extract_pdf_text", return_value=fake):
        r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "pdf"
    assert r["text"] == "PDF body"
    assert r["_meta"]["pages_count"] == 2


def test_dispatch_docx_via_extractor(tmp_path, monkeypatch):
    p = tmp_path / "test.docx"
    p.write_bytes(b"fake")
    fake = {"text": "doc body", "paragraphs_count": 3, "tables_count": 1,
            "chars": 8, "truncated": False, "file_name": "test.docx"}
    with patch("src.tools._docx_extract.extract_text", return_value=fake):
        r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "docx"
    assert r["text"] == "doc body"
    assert r["_meta"]["paragraphs_count"] == 3


def test_dispatch_xlsx(tmp_path):
    fake_data = {"Sheet1": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}
    p = tmp_path / "data.xlsx"
    p.write_bytes(b"fake")
    with patch("src.tools.excel.parse_xlsx", return_value=fake_data):
        r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "xlsx"
    assert "Sheet: Sheet1" in r["text"]
    assert "a=1" in r["text"]


def test_dispatch_image_via_ocr(tmp_path):
    p = tmp_path / "scan.png"
    p.write_bytes(b"fake")
    fake = {"text": "Распознанный текст", "_meta": {"lang": "rus+eng"}}
    with patch("src.tools.vision.ocr", return_value=fake):
        r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "image"
    assert "Распознанный" in r["text"]
    assert r["_meta"]["extraction_method"] == "ocr"


def test_dispatch_audio_via_transcribe(tmp_path):
    p = tmp_path / "call.mp3"
    p.write_bytes(b"fake")
    fake = {"text": "transcript here", "_meta": {"language": "ru", "duration_s": 120}}
    with patch("src.tools._audio_transcribe.transcribe", return_value=fake):
        r = file_extract.extract_text(str(p))
    assert r["file_kind"] == "audio"
    assert r["text"] == "transcript here"


# ---------- URL routing ----------

def test_dispatch_gdoc_url():
    url = "https://docs.google.com/document/d/1abcDEFghi567890_-XYZ12345/edit"
    fake = {"text": "gdoc text", "file_kind": "gdoc", "source": url,
            "chars": 9, "truncated": False, "_meta": {}}
    with patch("src.tools._gdoc_extract.extract", return_value=fake):
        r = file_extract.extract_text(url)
    assert r["file_kind"] == "gdoc"


def test_dispatch_gsheet_url():
    url = "https://docs.google.com/spreadsheets/d/1abcDEFghi567890_-XYZ12345/edit"
    fake = {"text": "sheet text", "file_kind": "gsheet", "source": url,
            "chars": 10, "truncated": False, "_meta": {}}
    with patch("src.tools._gsheet_extract.extract", return_value=fake):
        r = file_extract.extract_text(url)
    assert r["file_kind"] == "gsheet"


def test_dispatch_non_google_url_rejected():
    with pytest.raises(ValueError, match="not recognized as Google"):
        file_extract.extract_text("https://example.com/some.pdf")


# ---------- validation ----------

def test_empty_path_raises():
    with pytest.raises(ValueError, match="non-empty"):
        file_extract.extract_text("")
    with pytest.raises(ValueError, match="non-empty"):
        file_extract.extract_text("   ")


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        file_extract.extract_text("/nonexistent_path_xyz.txt")


def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "weird.xyz"
    p.write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        file_extract.extract_text(str(p))


def test_kind_override(tmp_path):
    """User can override auto-detect."""
    p = tmp_path / "no_extension"
    p.write_text("plain text content", encoding="utf-8")
    r = file_extract.extract_text(str(p), kind="text")
    assert r["file_kind"] == "text"
    assert "plain text" in r["text"]


# ---------- max_chars truncation ----------

def test_max_chars_truncates_text(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("a" * 1000, encoding="utf-8")
    r = file_extract.extract_text(str(p), max_chars=100)
    assert r["chars"] == 100
    assert r["truncated"] is True


def test_max_chars_no_truncation_when_under(tmp_path):
    p = tmp_path / "small.txt"
    p.write_text("short", encoding="utf-8")
    r = file_extract.extract_text(str(p), max_chars=1000)
    assert r["truncated"] is False
    assert r["chars"] == 5
