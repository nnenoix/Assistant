"""Google Slides tools — create, read, edit, export presentations.

Requires OAuth scope `https://www.googleapis.com/auth/presentations` and
GCP project must have `slides.googleapis.com` enabled.

Most common workflow: `create_from_template(template_id, replacements,
dest_title)` — clone an existing presentation, swap placeholders in
every slide. See replace_placeholders for the lower-level primitive.
"""
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "slides", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def create(
    title: str,
    parent_folder_id: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new empty Google Slides presentation. Returns {presentation_id, title, url}."""
    resp = _service(account).presentations().create(body={"title": title}).execute()
    pid = resp["presentationId"]
    if parent_folder_id:
        from src.tools import drive as _drive
        try:
            _drive.move(pid, parent_folder_id, account=account)
        except Exception:
            pass
    return {
        "presentation_id": pid,
        "title": resp.get("title"),
        "url": f"https://docs.google.com/presentation/d/{pid}/edit",
        "slide_count": len(resp.get("slides", [])),
    }


def create_from_template(
    template_id: str,
    replacements: dict[str, str],
    dest_title: str,
    dest_folder_id: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Copy `template_id`, rename copy to `dest_title`, replace all
    `{placeholder}` strings across every slide. The most common Slides
    workflow.

    Returns {presentation_id, replaced_count, url}.
    """
    from src.tools import drive as _drive
    # Step 1: copy via Drive
    copied = _drive.copy(template_id, new_name=dest_title, parent_id=dest_folder_id, account=account)
    new_pid = copied["id"]

    # Step 2: replace placeholders
    rep_result = replace_placeholders(new_pid, replacements, account=account)
    return {
        "presentation_id": new_pid,
        "title": dest_title,
        "url": f"https://docs.google.com/presentation/d/{new_pid}/edit",
        "replaced_count": rep_result["replaced_count"],
        "per_needle": rep_result["per_needle"],
    }


def _extract_slide_text(slide: dict) -> str:
    """Walk a slide's pageElements and concatenate text content."""
    parts: list[str] = []
    for el in slide.get("pageElements", []):
        shape = el.get("shape")
        if not shape:
            continue
        for te in shape.get("text", {}).get("textElements", []):
            tr = te.get("textRun")
            if tr and "content" in tr:
                parts.append(tr["content"])
    return "".join(parts)


def read(presentation_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read a presentation's title + per-slide text + structure.

    Returns {title, slides: [{slide_id, text, object_count}], _meta}.
    """
    pres = _service(account).presentations().get(presentationId=presentation_id).execute()
    slides_out = []
    for s in pres.get("slides", []):
        slides_out.append({
            "slide_id": s.get("objectId"),
            "text": _extract_slide_text(s),
            "object_count": len(s.get("pageElements", [])),
        })
    return {
        "title": pres.get("title"),
        "slides": slides_out,
        "_meta": {
            "presentation_id": presentation_id,
            "slide_count": len(slides_out),
            "empty_reason": None if slides_out else "no_slides",
        },
    }


def replace_placeholders(
    presentation_id: str,
    replacements: dict[str, str],
    match_case: bool = True,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Find-and-replace text across every slide.

    `replacements` example: {"{title}": "Q1 2026", "{client}": "Иван"}.
    Each entry becomes a separate `replaceAllText` batchUpdate request.
    Returns {replaced_count, per_needle}.
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
    resp = _service(account).presentations().batchUpdate(
        presentationId=presentation_id,
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
        "presentation_id": presentation_id,
        "replaced_count": total,
        "per_needle": per_needle,
    }


_VALID_LAYOUTS = {
    "BLANK", "CAPTION_ONLY", "TITLE", "TITLE_AND_BODY", "TITLE_AND_TWO_COLUMNS",
    "TITLE_ONLY", "SECTION_HEADER", "SECTION_TITLE_AND_DESCRIPTION", "ONE_COLUMN_TEXT",
    "MAIN_POINT", "BIG_NUMBER",
}


def add_slide(
    presentation_id: str,
    layout: str = "BLANK",
    position: int | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Add a new slide. `layout` is a predefined layout name; default BLANK.

    `position` is 0-indexed; None appends at the end.
    """
    if layout not in _VALID_LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; allowed: {sorted(_VALID_LAYOUTS)}")
    request: dict = {"createSlide": {
        "slideLayoutReference": {"predefinedLayout": layout},
    }}
    if position is not None:
        request["createSlide"]["insertionIndex"] = position
    resp = _service(account).presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": [request]},
    ).execute()
    new_id = resp.get("replies", [{}])[0].get("createSlide", {}).get("objectId")
    return {
        "ok": True,
        "presentation_id": presentation_id,
        "slide_id": new_id,
        "layout": layout,
        "position": position,
    }


def replace_image(
    presentation_id: str,
    image_object_id: str,
    new_url: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Replace an image by its object ID with a new image fetched from `new_url`.

    Find object IDs via `read()` and inspecting slides[].pageElements
    (image objects have `image` field).
    """
    _service(account).presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": [{"replaceImage": {
            "imageObjectId": image_object_id,
            "url": new_url,
            "imageReplaceMethod": "CENTER_INSIDE",
        }}]},
    ).execute()
    return {
        "ok": True,
        "presentation_id": presentation_id,
        "image_object_id": image_object_id,
        "new_url": new_url,
    }


def export_pdf(presentation_id: str, dest_path: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Export the presentation as PDF via Drive's files.export."""
    import io
    from googleapiclient.http import MediaIoBaseDownload
    from src.tools import drive as _drive

    request = _drive._service(account).files().export_media(
        fileId=presentation_id, mimeType="application/pdf",
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
        "presentation_id": presentation_id,
        "dest_path": str(p.resolve()),
        "bytes_written": p.stat().st_size,
    }
