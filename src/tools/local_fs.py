from pathlib import Path


def read_file(path: str, offset: int = 0, limit: int | None = None) -> dict:
    """Read a local text file (UTF-8). Returns {content, total_lines,
    offset, returned_lines, has_more}. Use offset+limit for CHUNKED reading
    of huge files — both are line-based (0-indexed offset, line-count limit).
    Without offset/limit: returns the whole file content.

    The tool output cap (~12k chars) truncates large files; chunked reads
    let the agent traverse the whole file deterministically.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if limit is None and offset == 0:
        return {
            "content": text,
            "total_lines": total,
            "offset": 0,
            "returned_lines": total,
            "has_more": False,
        }
    start = max(0, offset)
    end = total if limit is None else min(total, start + max(0, limit))
    chunk = "".join(lines[start:end])
    return {
        "content": chunk,
        "total_lines": total,
        "offset": start,
        "returned_lines": end - start,
        "has_more": end < total,
    }


def write_file(path: str, content: str) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p.resolve()), "bytes_written": len(content.encode("utf-8"))}


def list_dir(path: str) -> list[dict]:
    p = Path(path)
    return [
        {"name": e.name, "is_dir": e.is_dir(), "size": (e.stat().st_size if e.is_file() else None)}
        for e in sorted(p.iterdir())
    ]


def extract_pdf_text(path: str, pages: str | None = None, max_chars: int | None = None) -> dict:
    """Extract text from a local PDF using pdfplumber. Returns
    {pages_count, text, chars, truncated, file_name}.

    `pages` selects a range like "1-3" or "5" or "1,3,5" — None = all pages.
    `max_chars` caps the returned text (the tool wrapper truncates at 12k
    chars anyway; pass this to truncate earlier).
    """
    import pdfplumber

    p = Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    if p.suffix.lower() != ".pdf":
        return {"error": f"Not a PDF: {p.suffix}"}

    page_filter: set[int] | None = None
    if pages:
        page_filter = set()
        for part in pages.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                page_filter.update(range(int(a) - 1, int(b)))
            else:
                page_filter.add(int(part) - 1)

    chunks: list[str] = []
    pages_count = 0
    with pdfplumber.open(str(p)) as pdf:
        pages_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if page_filter is not None and i not in page_filter:
                continue
            t = page.extract_text() or ""
            chunks.append(f"--- page {i+1} ---\n{t}")
    full = "\n".join(chunks)
    truncated = False
    if max_chars and len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        truncated = True
    return {
        "file_name": p.name,
        "pages_count": pages_count,
        "chars": len(full),
        "text": full,
        "truncated": truncated,
    }


def image_info(path: str) -> dict:
    """Get image metadata + a small base64 data-URL preview suitable for
    sending to the agent as a multimodal input. Returns {width, height,
    format, bytes, data_url}. The image is downscaled to 1568px max side.
    """
    from PIL import Image
    import base64
    import io

    p = Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    try:
        with Image.open(p) as img:
            width, height = img.size
            fmt = img.format or p.suffix.lstrip(".").upper()
            img.thumbnail((1568, 1568))
            buf = io.BytesIO()
            (img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img).save(buf, format="PNG")
            data = buf.getvalue()
        return {
            "file_name": p.name,
            "format": fmt,
            "width": width,
            "height": height,
            "bytes": p.stat().st_size,
            "data_url": f"data:image/png;base64,{base64.b64encode(data).decode()}",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def walk_dir(path: str, max_files: int = 500, include_hidden: bool = False) -> dict:
    """Recursively list files in a directory. Returns {root, count, files:
    [{rel_path, size, suffix}]}. Cut off at `max_files` so a folder full of
    node_modules doesn't blow up.
    """
    root = Path(path)
    if not root.exists():
        return {"error": f"Not found: {path}"}
    if not root.is_dir():
        return {"error": f"Not a directory: {path}"}

    out: list[dict] = []
    for f in root.rglob("*"):
        if not include_hidden and any(part.startswith(".") for part in f.relative_to(root).parts):
            continue
        if f.is_file():
            try:
                size = f.stat().st_size
            except Exception:
                size = None
            out.append({
                "rel_path": str(f.relative_to(root)).replace("\\", "/"),
                "size": size,
                "suffix": f.suffix.lower(),
            })
            if len(out) >= max_files:
                break
    return {
        "root": str(root.resolve()),
        "count": len(out),
        "truncated": len(out) >= max_files,
        "files": out,
    }
