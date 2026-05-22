"""Google Doc / Sheet URL parser (Phase 15).

Parses URLs of the form:
  https://docs.google.com/document/d/<DOC_ID>/edit
  https://docs.google.com/document/d/<DOC_ID>/edit?usp=sharing
  https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit#gid=0
  https://drive.google.com/file/d/<FILE_ID>/view

Returns {kind, document_id} where kind ∈ {"gdoc", "gsheet", "gfile"}.
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict


class GdocUrlInfo(TypedDict):
    kind: str  # "gdoc" | "gsheet" | "gfile"
    document_id: str


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("gdoc", re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]{20,})")),
    ("gsheet", re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]{20,})")),
    ("gfile", re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]{20,})")),
    ("gfile", re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]{20,})")),
]


def parse(url: str) -> Optional[GdocUrlInfo]:
    """Extract {kind, document_id} from a Google URL. Returns None if not a recognized Google URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    for kind, pat in _PATTERNS:
        m = pat.search(url)
        if m:
            return {"kind": kind, "document_id": m.group(1)}
    return None


def is_google_url(s: str) -> bool:
    """Cheap test: does this look like a Google Docs/Drive URL?"""
    return parse(s) is not None
