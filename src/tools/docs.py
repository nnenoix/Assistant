"""Google Docs tools — create, read, edit, export documents.

Requires OAuth scope `https://www.googleapis.com/auth/documents` (added
to config.SCOPES in Phase 0). Existing tokens issued before that need
re-OAuth via /accounts UI → insufficient_scope on first call otherwise.

GCP project must also have `docs.googleapis.com` enabled (done in Phase 0).
"""
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "docs", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def create(title: str, parent_folder_id: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    """Create a new empty Google Doc. Returns {document_id, title, url}.

    If `parent_folder_id` is given, the new doc is moved into that Drive
    folder (Docs API itself creates in root; Drive's move handles relocation).
    """
    resp = _service(account).documents().create(body={"title": title}).execute()
    doc_id = resp["documentId"]
    if parent_folder_id:
        # Move via Drive
        from src.tools import drive as _drive
        try:
            _drive.move(doc_id, parent_folder_id, account=account)
        except Exception:
            pass  # creation succeeded; move is best-effort
    return {
        "document_id": doc_id,
        "title": resp.get("title"),
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }


def _extract_text_from_doc(doc: dict) -> tuple[str, list[dict]]:
    """Walk the document body and pull out concatenated plain text + heading
    structure. Returns (text, headings) where headings is
    [{level, text, start_index}].
    """
    text_parts: list[str] = []
    headings: list[dict] = []
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        heading_level = None
        if style.startswith("HEADING_"):
            try:
                heading_level = int(style.split("_")[1])
            except (IndexError, ValueError):
                heading_level = None
        para_text_parts = []
        for el in para.get("elements", []):
            tr = el.get("textRun")
            if tr and "content" in tr:
                para_text_parts.append(tr["content"])
        para_text = "".join(para_text_parts)
        text_parts.append(para_text)
        if heading_level is not None and para_text.strip():
            headings.append({
                "level": heading_level,
                "text": para_text.strip(),
                "start_index": element.get("startIndex", 0),
            })
    return "".join(text_parts), headings


def read(document_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read a Doc's title, full plain text, and heading structure.

    Returns {title, body_text, headings, _meta}. body_text is capped at
    50 000 chars for token-friendliness; _meta.body_truncated flags
    docs that exceeded that.
    """
    doc = _service(account).documents().get(documentId=document_id).execute()
    title = doc.get("title")
    full_text, headings = _extract_text_from_doc(doc)
    cap = 50_000
    body_truncated = len(full_text) > cap
    body_text = full_text[:cap]
    return {
        "title": title,
        "body_text": body_text,
        "headings": headings,
        "_meta": {
            "document_id": document_id,
            "char_count_total": len(full_text),
            "body_truncated": body_truncated,
            "heading_count": len(headings),
        },
    }


_HEADING_STYLES = {
    "h1": "HEADING_1",
    "h2": "HEADING_2",
    "h3": "HEADING_3",
    "h4": "HEADING_4",
    "h5": "HEADING_5",
    "h6": "HEADING_6",
    "title": "TITLE",
    "subtitle": "SUBTITLE",
    "normal": "NORMAL_TEXT",
}


def append_text(
    document_id: str,
    text: str,
    style: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Append `text` to the end of the document. Optional `style` for
    paragraph style: h1..h6, title, subtitle, normal.

    The trailing newline is added automatically so subsequent appends
    start on a new line.
    """
    svc = _service(account)
    # Get the current end of doc
    doc = svc.documents().get(documentId=document_id, fields="body.content").execute()
    content = doc.get("body", {}).get("content", [])
    end_index = 1  # Docs are 1-indexed; minimum is 1
    if content:
        last = content[-1]
        # endIndex points to one past the last char; subtract 1 to insert
        # BEFORE the trailing newline structural element
        end_index = max(1, last.get("endIndex", 1) - 1)

    payload = text if text.endswith("\n") else text + "\n"
    requests = [{
        "insertText": {
            "location": {"index": end_index},
            "text": payload,
        },
    }]
    if style:
        if style not in _HEADING_STYLES:
            raise ValueError(f"unknown style {style!r}; allowed: {sorted(_HEADING_STYLES)}")
        # Apply paragraph style to the inserted range
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": end_index, "endIndex": end_index + len(payload)},
                "paragraphStyle": {"namedStyleType": _HEADING_STYLES[style]},
                "fields": "namedStyleType",
            },
        })

    svc.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    return {
        "ok": True,
        "document_id": document_id,
        "appended_chars": len(payload),
        "style": style,
    }


def replace_text(
    document_id: str,
    replacements: dict[str, str],
    match_case: bool = True,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Replace text across the whole document.

    `replacements` is a dict like {"{client}": "Иван Иванов", "{date}": "2026-05-20"}.
    Each entry becomes a separate replaceAllText batchUpdate request.
    """
    if not replacements:
        return {"ok": True, "replaced_count": 0, "_meta": {"empty_reason": "no_replacements"}}
    requests = [
        {"replaceAllText": {
            "containsText": {"text": needle, "matchCase": match_case},
            "replaceText": replacement,
        }}
        for needle, replacement in replacements.items()
    ]
    resp = _service(account).documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    total = 0
    per_needle = []
    for needle, reply in zip(replacements.keys(), resp.get("replies", [])):
        n = reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
        total += n
        per_needle.append({"needle": needle, "occurrences": n})
    return {
        "ok": True,
        "document_id": document_id,
        "replaced_count": total,
        "per_needle": per_needle,
    }


def insert_table(
    document_id: str,
    rows: int,
    cols: int,
    position_index: int | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Insert a table at `position_index`. If position_index is None,
    appends at the end of the document.
    """
    svc = _service(account)
    if position_index is None:
        doc = svc.documents().get(documentId=document_id, fields="body.content").execute()
        content = doc.get("body", {}).get("content", [])
        position_index = 1
        if content:
            position_index = max(1, content[-1].get("endIndex", 1) - 1)

    svc.documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{
            "insertTable": {
                "location": {"index": position_index},
                "rows": rows,
                "columns": cols,
            },
        }]},
    ).execute()
    return {
        "ok": True,
        "document_id": document_id,
        "rows": rows,
        "cols": cols,
        "inserted_at": position_index,
    }


def export_pdf(document_id: str, dest_path: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Export the document as PDF and save to `dest_path`.

    Uses Drive's `files.export(mimeType='application/pdf')` since the Docs
    API itself doesn't export.
    """
    import io
    from googleapiclient.http import MediaIoBaseDownload
    from src.tools import drive as _drive

    request = _drive._service(account).files().export_media(
        fileId=document_id, mimeType="application/pdf",
    )
    p = Path(dest_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with io.FileIO(str(p), "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return {
        "ok": True,
        "document_id": document_id,
        "dest_path": str(p.resolve()),
        "bytes_written": p.stat().st_size,
    }
