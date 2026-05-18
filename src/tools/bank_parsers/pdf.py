"""PDF text extraction.

Strategy mirrors crates/bank-parsers/src/pdf.rs:
    1. Primary: pypdf (closer to Rust's pdf-extract — both walk the content
       stream without layout detection, so the resulting text shape matches).
    2. Fallback: pdfplumber if pypdf returned nothing (it sometimes handles
       encrypted/unusual PDFs that pypdf chokes on).
    3. Last resort: external `pdftotext` (poppler) — for encrypted/no-ToUnicode
       PDFs. Same use case as the Rust fallback.

Returns decoded UTF-8 text. Empty result is allowed; the bank parser then
returns no transactions, which the Rust orchestrator surfaces as BadInput.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def extract_text(path: str | Path) -> str:
    p = Path(path)
    for extractor in (_extract_with_pypdf, _extract_with_pdfplumber, _extract_with_pdftotext):
        text = extractor(p)
        if text.strip():
            return text
    return ""


def _extract_with_pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _extract_with_pdfplumber(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception:
        return ""


def _extract_with_pdftotext(path: Path) -> str:
    if not shutil.which("pdftotext"):
        return ""
    try:
        r = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", "-upw", "", str(path), "-"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""
