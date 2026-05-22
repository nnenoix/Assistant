"""Universal file text extraction dispatcher (Phase 15).

Routes by file extension or URL pattern to the right extractor:
  .txt/.md/.csv     → local_fs.read_file (existing)
  .pdf              → local_fs.extract_pdf_text (existing, pdfplumber)
  .docx             → _docx_extract.extract_text (new, python-docx)
  .xlsx             → excel.parse_xlsx (existing, openpyxl)
  .png/.jpg/.jpeg   → vision.ocr (existing, Tesseract)
  .mp3/.m4a/.wav    → _audio_transcribe.transcribe (new, Whisper API)
  Google Doc URL    → _gdoc_extract (15D)
  Google Sheet URL  → _gsheet_extract (15D)

Returns unified shape:
  {text, file_kind, source_path, chars, truncated, _meta}

The analyzer (file_analyze_ensemble) is kind-agnostic and consumes this.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.tools import _gdoc_url


# Extension → kind mapping
_EXT_KIND = {
    ".txt": "text",
    ".md": "text",
    ".csv": "text",
    ".log": "text",
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp3": "audio",
    ".m4a": "audio",
    ".wav": "audio",
    ".ogg": "audio",
}


def extract_text(path_or_url: str, kind: str | None = None, max_chars: int | None = None) -> dict:
    """Universal text extraction. Auto-routes by extension or URL.

    Args:
      path_or_url: local file path OR Google Docs/Sheets URL.
      kind: optional override of auto-detect ("text"/"pdf"/"docx"/"xlsx"/"image"/"audio"/"gdoc"/"gsheet").
      max_chars: cap output (extractors truncate at this and set _meta.truncated=True).

    Returns:
      {text, file_kind, source, chars, truncated, _meta}
      _meta varies by kind — preserves extractor-specific fields (page_count, paragraphs, etc).

    Raises:
      ValueError: invalid path_or_url, unsupported extension
      FileNotFoundError: local path doesn't exist
      Tool-specific errors: PDF can't open, audio missing API key, etc.
    """
    if not isinstance(path_or_url, str) or not path_or_url.strip():
        raise ValueError("path_or_url must be a non-empty string")
    s = path_or_url.strip()

    # 1. URL — Google Docs/Sheets/Drive
    if s.startswith(("http://", "https://")):
        gurl = _gdoc_url.parse(s)
        if not gurl:
            raise ValueError(
                f"URL not recognized as Google Docs/Sheets/Drive: {s[:100]}. "
                f"Only Google URLs are supported in v1 (use web_fetch for arbitrary web pages)."
            )
        effective_kind = kind or gurl["kind"]
        return _route_url(s, gurl["document_id"], effective_kind, max_chars)

    # 2. Local file
    p = Path(s)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {s}")

    effective_kind = kind or _EXT_KIND.get(p.suffix.lower())
    if not effective_kind:
        raise ValueError(
            f"Unsupported file extension {p.suffix!r}. Supported: "
            f"{sorted(set(_EXT_KIND.values()))}. Override via kind= param if needed."
        )
    return _route_local(p, effective_kind, max_chars)


def _apply_max_chars(text: str, max_chars: int | None) -> tuple[str, bool]:
    """Truncate `text` to `max_chars` if exceeded. Returns (text, was_truncated).

    Raises ValueError on negative max_chars — `text[:-N]` would silently drop
    trailing chars, which is not the truncation semantic the caller asked for.
    """
    if max_chars is None:
        return text, False
    if max_chars < 0:
        raise ValueError(f"max_chars must be non-negative, got {max_chars}")
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _route_local(p: Path, kind: str, max_chars: int | None) -> dict:
    """Route a local file to the right extractor based on resolved kind."""
    fn = _LOCAL_ROUTERS.get(kind)
    if fn is None:
        raise ValueError(f"Unsupported kind={kind!r} for local file")
    return fn(p, max_chars)


def _route_url(url: str, doc_id: str, kind: str, max_chars: int | None) -> dict:
    """Route a Google URL to the right extractor."""
    if kind == "gdoc":
        from src.tools import _gdoc_extract
        return _gdoc_extract.extract(doc_id, source_url=url, max_chars=max_chars)
    if kind == "gsheet":
        from src.tools import _gsheet_extract
        return _gsheet_extract.extract(doc_id, source_url=url, max_chars=max_chars)
    if kind == "gfile":
        # Drive file — fetch metadata, dispatch by mime
        from src.tools import drive
        meta = drive.get_metadata(doc_id)
        mime = meta.get("mimeType", "")
        if "spreadsheet" in mime:
            from src.tools import _gsheet_extract
            return _gsheet_extract.extract(doc_id, source_url=url, max_chars=max_chars)
        if "document" in mime:
            from src.tools import _gdoc_extract
            return _gdoc_extract.extract(doc_id, source_url=url, max_chars=max_chars)
        raise ValueError(
            f"Drive file mime={mime!r} not supported. Native Google Doc/Sheet only in v1."
        )
    raise ValueError(f"Unsupported kind={kind!r} for URL")


# ============== per-kind adapters (compose existing tools) ==============

def _extract_text_file(p: Path, max_chars: int | None) -> dict:
    from src.tools import local_fs
    r = local_fs.read_file(str(p))
    text, truncated = _apply_max_chars(r.get("content", "") or "", max_chars)
    return {
        "text": text,
        "file_kind": "text",
        "source": str(p),
        "chars": len(text),
        "truncated": truncated,
        "_meta": {
            "total_lines": r.get("total_lines"),
            "file_name": p.name,
        },
    }


def _extract_pdf(p: Path, max_chars: int | None) -> dict:
    from src.tools import local_fs
    r = local_fs.extract_pdf_text(str(p), max_chars=max_chars)
    if "error" in r:
        raise ValueError(r["error"])
    return {
        "text": r.get("text", ""),
        "file_kind": "pdf",
        "source": str(p),
        "chars": r.get("chars", 0),
        "truncated": r.get("truncated", False),
        "_meta": {
            "pages_count": r.get("pages_count"),
            "file_name": r.get("file_name"),
        },
    }


def _extract_docx(p: Path, max_chars: int | None) -> dict:
    from src.tools import _docx_extract
    r = _docx_extract.extract_text(str(p), max_chars=max_chars)
    return {
        "text": r["text"],
        "file_kind": "docx",
        "source": str(p),
        "chars": r["chars"],
        "truncated": r["truncated"],
        "_meta": {
            "paragraphs_count": r["paragraphs_count"],
            "tables_count": r["tables_count"],
            "file_name": r["file_name"],
        },
    }


def _extract_xlsx(p: Path, max_chars: int | None) -> dict:
    from src.tools import excel
    r = excel.parse_xlsx(str(p))
    # excel returns {sheet_name: [row_dicts]} OR [row_dicts] if sheet specified.
    # Flatten to text representation for downstream LLM.
    if isinstance(r, dict):
        parts = []
        for sheet, rows in r.items():
            parts.append(f"=== Sheet: {sheet} ===")
            for row in rows:
                parts.append(" | ".join(f"{k}={v}" for k, v in row.items()))
        text = "\n".join(parts)
    else:
        text = "\n".join(" | ".join(f"{k}={v}" for k, v in row.items()) for row in r)
    text, truncated = _apply_max_chars(text, max_chars)
    return {
        "text": text,
        "file_kind": "xlsx",
        "source": str(p),
        "chars": len(text),
        "truncated": truncated,
        "_meta": {"file_name": p.name},
    }


def _extract_image(p: Path, max_chars: int | None) -> dict:
    """Image OCR via Tesseract. For multimodal vision (image-to-text via
    Claude), file_analyze_ensemble has a separate path that passes data-URL
    directly. This extractor returns OCR-only text."""
    from src.tools import vision
    r = vision.ocr(str(p))
    text, truncated = _apply_max_chars(r.get("text", "") or "", max_chars)
    return {
        "text": text,
        "file_kind": "image",
        "source": str(p),
        "chars": len(text),
        "truncated": truncated,
        "_meta": r.get("_meta", {}) | {"file_name": p.name, "extraction_method": "ocr"},
    }


def _extract_audio(p: Path, max_chars: int | None) -> dict:
    """Audio transcription. Defers to _audio_transcribe if available."""
    from src.tools import _audio_transcribe
    r = _audio_transcribe.transcribe(str(p))
    text, truncated = _apply_max_chars(r.get("text", "") or "", max_chars)
    return {
        "text": text,
        "file_kind": "audio",
        "source": str(p),
        "chars": len(text),
        "truncated": truncated,
        "_meta": (r.get("_meta") or {}) | {"file_name": p.name},
    }


# Lookup table for _route_local — defined AFTER all _extract_* are defined
_LOCAL_ROUTERS = {
    "text":  _extract_text_file,
    "pdf":   _extract_pdf,
    "docx":  _extract_docx,
    "xlsx":  _extract_xlsx,
    "image": _extract_image,
    "audio": _extract_audio,
}
