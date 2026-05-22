"""Google Sheet text extraction (Phase 15D).

Strategy: `sheets.summarize(spreadsheet_id)` for structural overview (title +
per-tab dims + header row + sample rows). For deep analysis the agent should
follow up with `sheets.read_range` or specific extraction; this extractor
provides the structural overview as text.
"""
from __future__ import annotations

import json


def extract(document_id: str, source_url: str = "", max_chars: int | None = None) -> dict:
    """Extract structural summary of a Google Sheet as text.

    Returns {text, file_kind: "gsheet", source, chars, truncated, _meta}.
    `text` is a markdown-ish rendering of title + each tab's name, dimensions,
    headers, and first ~5 data rows.

    For full data extraction the agent should use `sheets_read_range` /
    `sheets_query` / `sheets_bulk_metric` directly — this extractor is for the
    "give me an overview to analyze" use case.
    """
    from src.tools import sheets

    summary = sheets.summarize(document_id, sample_rows=10)

    parts: list[str] = []
    parts.append(f"# {summary.get('title', '(untitled)')}")
    parts.append(f"spreadsheet_id: {summary.get('spreadsheet_id')}")
    parts.append("")

    for s in summary.get("sheets", []):
        parts.append(f"## Sheet: {s.get('name')}")
        parts.append(f"  size: {s.get('rows_total')} rows × {s.get('cols_total')} cols")
        headers = s.get("headers")
        if headers:
            parts.append(f"  headers: {' | '.join(str(h) for h in headers)}")
        sample = s.get("sample_rows") or []
        if sample:
            parts.append("  sample rows:")
            for row in sample:
                parts.append(f"    {' | '.join(str(c) for c in row)}")
        parts.append("")

    text = "\n".join(parts)
    truncated = False
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "text": text,
        "file_kind": "gsheet",
        "source": source_url or f"gsheet:{document_id}",
        "chars": len(text),
        "truncated": truncated,
        "_meta": {
            "spreadsheet_id": document_id,
            "title": summary.get("title"),
            "tabs_count": len(summary.get("sheets", [])),
            "extraction_method": "sheets_summarize",
        },
    }
