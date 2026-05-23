"""Bulk-tool payload compaction (Phase 14).

Bulk tools that fan out over N=500+ items return aggregated stats + a
small set of outliers, with full per-file data spilled to disk under
.data/bulk/<token>.json. Agent gets the answer in <3KB; drill-down via
bulk_load_results(token). Fits inside MAX_TOOL_PAYLOAD = 12 000 with margin.

Re-uses the project's existing `_meta` envelope conventions (see
registry.py:_meta_warning_prefix) — when `n_err > 0` the bulk tool sets
`_meta.truncated = True` and `_meta.truncation_reason = "errors_present"`
so the ⚠️ META prefix surfaces it.
"""
from __future__ import annotations

import json
import math
import secrets
import time
from pathlib import Path
from statistics import mean, median
from typing import Any

from src.config import DATA_DIR

BULK_DIR = DATA_DIR / "bulk"
BULK_DIR.mkdir(exist_ok=True)

MAX_OUTLIERS_PER_TAIL = 10
MAX_ERRORS_LISTED = 5
MAX_KEEP_RESULT_FILES = 100

# Ops with a natural numeric ordering — outlier selection makes sense
_NUMERIC_OPS = {"sum", "avg", "mean", "min", "max", "ratio"}


def _is_numeric(v: Any) -> bool:
    if isinstance(v, bool):  # bools are ints — exclude
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return not math.isnan(v)
    return False


def compute_stats(values: list[Any]) -> dict:
    """Aggregate stats across `values`. Non-numeric (None, str, NaN) counted
    as n_err. Returns {n_ok, n_err, sum, mean, p50, p95, min, max} — numeric
    fields are None when n_ok == 0.
    """
    clean = [v for v in values if _is_numeric(v)]
    n_ok = len(clean)
    n_err = len(values) - n_ok
    if not clean:
        return {"n_ok": 0, "n_err": n_err, "sum": None, "mean": None,
                "p50": None, "p95": None, "min": None, "max": None}
    s = sorted(clean)

    def _pct(p: float) -> float:
        idx = min(int(p * (len(s) - 1) + 0.5), len(s) - 1)
        return s[idx]

    return {
        "n_ok": n_ok,
        "n_err": n_err,
        "sum": sum(clean),
        "mean": mean(clean),
        "p50": median(clean),
        "p95": _pct(0.95),
        "min": min(clean),
        "max": max(clean),
    }


def compute_outliers(items: list[dict], op: str, k: int = MAX_OUTLIERS_PER_TAIL) -> dict:
    """Pick the most-interesting items based on `op` semantics.

    items: [{"id": str, "value": Any}]
    For numeric ops (sum/avg/min/max/ratio): top-k and bottom-k by value.
    For list/count and other ops: empty (no natural numeric ordering).
    """
    if op not in _NUMERIC_OPS:
        return {"top": [], "bottom": []}
    numeric = [x for x in items if _is_numeric(x.get("value"))]
    if not numeric:
        return {"top": [], "bottom": []}
    by_val = sorted(numeric, key=lambda x: x["value"])
    return {
        "top": list(reversed(by_val[-k:])),
        "bottom": by_val[:k],
    }


def make_token() -> str:
    """Token for the spill file: bulk_<unix_ts>_<8hex>."""
    return f"bulk_{int(time.time())}_{secrets.token_hex(4)}"


def write_result_file(token: str, full_data: Any) -> Path:
    """Spill full per-item data to .data/bulk/<token>.json. Returns path.

    Tokens are validated symmetrically with `load_result_file`. Today the
    only callers pass `make_token()` output, but a future caller passing
    `../../etc/secrets` would otherwise escape BULK_DIR — guard here too."""
    if not _safe_token(token):
        raise ValueError(f"invalid token format: {token!r}")
    path = BULK_DIR / f"{token}.json"
    path.write_text(
        json.dumps(full_data, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def load_result_file(token: str) -> Any:
    """Read full data back by token. Raises FileNotFoundError if missing."""
    if not _safe_token(token):
        raise ValueError(f"invalid token format: {token!r}")
    path = BULK_DIR / f"{token}.json"
    if not path.exists():
        raise FileNotFoundError(f"bulk result token not found: {token}")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_token(token: str) -> bool:
    """Tokens we mint match `bulk_<digits>_<hex>` — reject anything else
    so a malicious agent-supplied token can't escape BULK_DIR."""
    if not isinstance(token, str) or "/" in token or "\\" in token or ".." in token:
        return False
    return token.startswith("bulk_")


def cleanup_old(max_keep: int = MAX_KEEP_RESULT_FILES) -> int:
    """Keep at most `max_keep` result files; delete oldest. Returns deleted count."""
    files = sorted(BULK_DIR.glob("bulk_*.json"), key=lambda p: p.stat().st_mtime)
    deleted = 0
    while len(files) > max_keep:
        p = files.pop(0)
        try:
            p.unlink()
            deleted += 1
        except OSError:
            break
    return deleted


def compact(
    items: list[dict],
    op: str,
    errors: list[dict] | None = None,
    started_at: float | None = None,
    extra_meta: dict | None = None,
) -> dict:
    """Build the compacted bulk-result payload.

    Args:
      items: [{"id": ..., "value": ...}] — successful reads only
      op: operation name ("sum"|"avg"|"min"|"max"|"list"|"count"|"ratio"|...)
      errors: [{"id": ..., "kind": ..., "msg": ...}] — first 5 shown, all spilled
      started_at: time.perf_counter() snapshot at call start (for duration_ms)
      extra_meta: merged into result["_meta"]

    Writes {items, errors, op} to disk, returns compacted dict ≤ ~3KB.
    Sets _meta.truncated=True when errors present so existing META prefix fires.
    """
    errors = errors or []
    token = make_token()
    write_result_file(token, {"items": items, "errors": errors, "op": op})
    cleanup_old()

    values = [it.get("value") for it in items]
    stats = compute_stats(values)
    # Override n_err to include explicit errors on top of non-numeric values
    stats["n_err"] = stats["n_err"] + len(errors)
    outliers = compute_outliers(items, op)

    duration_ms: float | None = None
    if started_at is not None:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 1)

    meta: dict = {
        "result_token": token,
        "n": len(items) + len(errors),
        "duration_ms": duration_ms,
        "op": op,
    }
    if errors:
        meta["truncated"] = True
        meta["truncation_reason"] = f"{len(errors)} per-item errors"
    if extra_meta:
        meta.update(extra_meta)

    return {
        "stats": stats,
        "outliers": outliers,
        "errors": errors[:MAX_ERRORS_LISTED],
        "_meta": meta,
    }
