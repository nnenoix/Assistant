from pathlib import Path


def read_file(path: str) -> str:
    """Read a local text file (UTF-8). For binary, use drive.upload pattern instead."""
    return Path(path).read_text(encoding="utf-8")


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
