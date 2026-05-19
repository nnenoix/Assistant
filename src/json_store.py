"""Tiny JSON-on-disk persistence helper.

Six call sites in this codebase do exactly the same dance: load JSON
(with a default on missing/corrupt) → mutate → save with UTF-8 + indent.
This module centralizes it.

Existing modules with their own variants (notes, chats, sheets) are not
migrated — they predate this helper and have slightly different semantics
(e.g., chats writes JSONL, not pretty JSON).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    """Load JSON from `path`. Returns `default` on missing file or parse error."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    """Save `data` as pretty UTF-8 JSON to `path`. Parent dirs must exist."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso_z() -> str:
    """Current UTC time as ISO-8601 with trailing 'Z'."""
    return _dt.datetime.utcnow().isoformat() + "Z"
