"""Shared identifier-safety check.

Multiple call sites build filesystem paths from caller-supplied names
(`infra.py` MDM table names, `_vendor_helpers.py` cache keys,
`scripts/migrate_jsonl_to_pg.py` MDM-file scan). All three need the
same defense against path-traversal payloads — slashes, `..`, control
chars — and all three independently grew the same regex. This module
is the one source of truth.

Why the chosen alphabet:
    [A-Za-z0-9_-]   — covers alnum, underscores (`tenant_id`), dashes
                       (`kebab-case`). Excludes dots (no `.json` injection),
                       slashes (no traversal), spaces, control chars.
    {1,64}          — non-empty AND length-capped (no OS path-limit DoS).
"""
from __future__ import annotations

import re

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def is_safe_id(s: str) -> bool:
    """Return True iff `s` matches the safe-identifier regex.

    Callers typically wrap this in their own error envelope:
        if not is_safe_id(table):
            return {"ok": False, "error_kind": "bad_input",
                    "error": f"invalid name {table!r}"}
    """
    return isinstance(s, str) and bool(_SAFE_ID_RE.match(s))
