"""Typed report storage + cross-report merging.

Notes vs reports:
  - `notes` module stores free-form text observations ("the WB token expires
    Aug 10", "Варычев pays vendors via tochka"). Searchable by semantic
    similarity.
  - This module stores STRUCTURED data — parsed bank statements, ABC analyses,
    aggregated rows. Reports live in `.data/reports/<kind>/<name>.json` with
    metadata. Can be combined (merged on a key + summed) to create a unified
    view across multiple source reports.

Typical workflow:
  1. Parse Варычев's Альфа statement (Nov) → save_report("varychev_alfa_nov_2025", "bank", rows)
  2. Parse Варычев's Альфа statement (Dec) → save_report("varychev_alfa_dec_2025", ...)
  3. Combine: combine_reports(["varychev_alfa_nov_2025", "varychev_alfa_dec_2025"], key="counterparty", sum=["amount"])
  4. Analyze: abc_analysis(combined.rows, ...)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.json_store import now_iso_z, read_json, write_json


REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Kind dirs we check before falling back to rglob in load_report. Keep in
# sync with what callers actually use.
_KNOWN_KINDS = ("bank", "abc", "sales", "expenses", "combined")


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _kind_dir(kind: str) -> Path:
    p = REPORTS_DIR / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_report(
    name: str,
    kind: str,
    data: Any,
    metadata: dict | None = None,
) -> dict:
    """Persist a structured report to disk. Use `kind` to namespace (e.g.
    'bank' for bank statements, 'abc' for analyses, 'sales' for sales data).
    `data` can be any JSON-serializable structure — typically a list of dicts
    (rows) or a dict with stats + rows.

    Returns {name, kind, path, saved_at, bytes}.
    """
    safe = _safe_name(name)
    payload = {
        "name": safe,
        "kind": kind,
        "saved_at": now_iso_z(),
        "metadata": metadata or {},
        "data": data,
    }
    p = _kind_dir(kind) / f"{safe}.json"
    write_json(p, payload)
    return {
        "name": safe,
        "kind": kind,
        "path": str(p.resolve()),
        "saved_at": payload["saved_at"],
        "bytes": p.stat().st_size,
    }


def load_report(name: str, kind: str | None = None) -> dict:
    """Load a saved report. If `kind` is given, looks only in that namespace;
    otherwise scans all kinds. Returns the full payload {name, kind, saved_at,
    metadata, data}. Raises FileNotFoundError if not found.
    """
    safe = _safe_name(name)
    if kind:
        candidates = [_kind_dir(kind) / f"{safe}.json"]
    else:
        # Try known kinds first (O(1) per kind), only rglob if all miss
        candidates = [REPORTS_DIR / k / f"{safe}.json" for k in _KNOWN_KINDS]
        candidates += list(REPORTS_DIR.rglob(f"{safe}.json"))
    for p in candidates:
        payload = read_json(p, None)
        if payload is not None:
            return payload
    raise FileNotFoundError(f"Report {name!r} not found (kind={kind!r})")


def list_reports(kind: str | None = None, limit: int = 50) -> dict:
    """List saved reports. Returns [{name, kind, saved_at, bytes,
    metadata_keys}]. Newest first.
    """
    if kind:
        paths = list(_kind_dir(kind).glob("*.json"))
    else:
        paths = list(REPORTS_DIR.rglob("*.json"))
    out: list[dict] = []
    for p in paths:
        payload = read_json(p, None)
        if payload is None:
            continue
        out.append({
            "name": payload.get("name", p.stem),
            "kind": payload.get("kind", p.parent.name),
            "saved_at": payload.get("saved_at"),
            "bytes": p.stat().st_size,
            "metadata_keys": list((payload.get("metadata") or {}).keys()),
        })
    out.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return {"count": len(out), "reports": out[:limit]}


def delete_report(name: str, kind: str | None = None) -> dict:
    """Delete a saved report. Returns {deleted, name}."""
    safe = _safe_name(name)
    if kind:
        candidates = [_kind_dir(kind) / f"{safe}.json"]
    else:
        candidates = list(REPORTS_DIR.rglob(f"{safe}.json"))
    deleted: list[str] = []
    for p in candidates:
        if p.exists():
            p.unlink()
            deleted.append(str(p))
    return {"deleted": len(deleted) > 0, "name": safe, "files": deleted}


def combine_reports(
    names: list[str],
    merge_key: str,
    sum_cols: list[str] | None = None,
    keep_first_cols: list[str] | None = None,
    kind: str | None = None,
    save_as: str | None = None,
) -> dict:
    """Merge multiple structured reports into one unified row set, keyed by
    `merge_key`. Rows with the same key value across reports are combined:
      - Columns in `sum_cols` are summed (revenue, qty, amount_cents, etc.)
      - Columns in `keep_first_cols` keep the value from the first report
        where the key first appears.
      - All other columns are preserved as-is from the first occurrence.

    Each input report's `data` must be either:
      - a list of dicts (rows directly), or
      - a dict with a `rows` field that is a list of dicts.

    Returns {merged_count, source_count, sources: [{name, rows}], rows: [...]}.
    If `save_as` is given, the merged result is also saved as a new report
    (kind defaults to 'combined' if save_as is set).
    """
    sum_cols = sum_cols or []
    keep_first_cols = keep_first_cols or []
    sources: list[dict] = []
    by_key: dict[Any, dict] = {}

    for name in names:
        try:
            payload = load_report(name, kind=kind)
        except FileNotFoundError:
            sources.append({"name": name, "rows": 0, "error": "not found"})
            continue
        data = payload.get("data")
        rows = data if isinstance(data, list) else (data.get("rows", []) if isinstance(data, dict) else [])
        sources.append({"name": name, "rows": len(rows)})

        for row in rows:
            key = row.get(merge_key)
            if key is None:
                continue
            if key not in by_key:
                # Initialize merged record from this row, copying scalars
                merged: dict = dict(row)
                # Initialize sum cols
                for c in sum_cols:
                    merged[c] = float(row.get(c) or 0)
                by_key[key] = merged
            else:
                target = by_key[key]
                for c in sum_cols:
                    target[c] = float(target.get(c) or 0) + float(row.get(c) or 0)
                # keep_first_cols already preserved by virtue of being set on first encounter

    merged_rows = list(by_key.values())
    result = {
        "merged_count": len(merged_rows),
        "source_count": len(sources),
        "sources": sources,
        "merge_key": merge_key,
        "sum_cols": sum_cols,
        "rows": merged_rows,
    }

    if save_as:
        save_kind = kind or "combined"
        save_report(
            name=save_as,
            kind=save_kind,
            data=merged_rows,
            metadata={
                "combined_from": names,
                "merge_key": merge_key,
                "sum_cols": sum_cols,
                "source_count": len(sources),
            },
        )
        result["saved_as"] = save_as

    return result
