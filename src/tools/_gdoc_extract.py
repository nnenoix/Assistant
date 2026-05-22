"""Google Doc text extraction (Phase 15D).

Uses `docs.read(document_id)` directly — Docs API returns body_text + headings
out of the box, no need for PDF round-trip. Much faster and preserves
heading structure.
"""
from __future__ import annotations


def extract(document_id: str, source_url: str = "", max_chars: int | None = None) -> dict:
    """Extract text from a Google Doc by document ID.

    Returns unified shape: {text, file_kind: "gdoc", source, chars, truncated, _meta}.
    `_meta` includes title, headings, and (if available) char_count_total.

    Raises:
      docs API errors propagate up — typically HttpError on access denied
      or document not found.
    """
    from src.tools import docs

    r = docs.read(document_id)
    body = r.get("body_text") or ""
    truncated = bool(r.get("_meta", {}).get("body_truncated"))
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    return {
        "text": body,
        "file_kind": "gdoc",
        "source": source_url or f"gdoc:{document_id}",
        "chars": len(body),
        "truncated": truncated,
        "_meta": {
            "document_id": document_id,
            "title": r.get("title"),
            "headings": r.get("headings", []),
            "char_count_total": r.get("_meta", {}).get("char_count_total"),
            "extraction_method": "docs_api",
        },
    }
