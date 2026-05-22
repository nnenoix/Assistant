"""Local PDF generation via reportlab.

For generating PDFs from agent output without going through Google Docs:
quick reports, tables, signed receipts, etc.
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


def _register_cyrillic_font() -> str:
    """Try to register a Cyrillic-friendly font; return font name to use."""
    candidates = [
        ("DejaVuSans", r"C:\Windows\Fonts\DejaVuSans.ttf"),
        ("Arial", r"C:\Windows\Fonts\arial.ttf"),
        ("Verdana", r"C:\Windows\Fonts\verdana.ttf"),
        ("Tahoma", r"C:\Windows\Fonts\tahoma.ttf"),
        # Linux fallbacks
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for name, path in candidates:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                continue
    return "Helvetica"  # ASCII-only fallback


_FONT_NAME = _register_cyrillic_font()


def create_pdf(
    content,
    dest_path: str,
    kind: str = "text",
    title: str | None = None,
) -> dict:
    """Build a PDF at `dest_path`.

    `kind`:
      - "text" — content is a string. Treated as plain paragraphs (split on
        blank lines).
      - "table" — content is {"headers": [...], "rows": [[...], ...]}.
      - "report" — content is structured: {
            "title": "...",
            "sections": [
                {"heading": "Section 1", "paragraphs": ["text..."]},
                {"heading": "Section 2", "table": {"headers": [...], "rows": [...]}},
            ]
        }

    Returns {ok, dest_path, bytes_written, page_count?}.
    """
    p = Path(dest_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    # Patch font for Cyrillic
    for st in ("Title", "Heading1", "Heading2", "BodyText", "Normal"):
        if st in styles.byName:
            styles.byName[st].fontName = _FONT_NAME

    doc = SimpleDocTemplate(
        str(p),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=title or kind,
    )
    story: list = []

    if kind == "text":
        if not isinstance(content, str):
            raise ValueError("kind='text' expects a string in `content`")
        if title:
            story.append(Paragraph(title, styles["Title"]))
            story.append(Spacer(1, 8))
        for chunk in content.split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                story.append(Paragraph(chunk.replace("\n", "<br/>"), styles["BodyText"]))
                story.append(Spacer(1, 6))

    elif kind == "table":
        if not isinstance(content, dict) or "headers" not in content or "rows" not in content:
            raise ValueError("kind='table' expects {'headers': [...], 'rows': [[...], ...]}")
        if title:
            story.append(Paragraph(title, styles["Title"]))
            story.append(Spacer(1, 8))
        data = [content["headers"]] + [list(r) for r in content["rows"]]
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), _FONT_NAME),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)

    elif kind == "report":
        if not isinstance(content, dict):
            raise ValueError("kind='report' expects structured dict")
        rep_title = content.get("title") or title or "Report"
        story.append(Paragraph(rep_title, styles["Title"]))
        story.append(Spacer(1, 12))
        for sect in content.get("sections", []):
            if heading := sect.get("heading"):
                story.append(Paragraph(heading, styles["Heading1"]))
                story.append(Spacer(1, 4))
            for para in sect.get("paragraphs", []) or []:
                story.append(Paragraph(para.replace("\n", "<br/>"), styles["BodyText"]))
                story.append(Spacer(1, 6))
            if tbl := sect.get("table"):
                data = [tbl["headers"]] + [list(r) for r in tbl["rows"]]
                t = Table(data, repeatRows=1)
                t.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, -1), _FONT_NAME),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ]))
                story.append(t)
                story.append(Spacer(1, 8))
            if sect.get("page_break_after"):
                story.append(PageBreak())

    else:
        raise ValueError(f"unknown kind {kind!r}; allowed: text, table, report")

    doc.build(story)
    return {
        "ok": True,
        "dest_path": str(p.resolve()),
        "bytes_written": p.stat().st_size,
        "kind": kind,
        "font_used": _FONT_NAME,
    }
