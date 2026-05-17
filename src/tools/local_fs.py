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
