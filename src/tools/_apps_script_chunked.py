"""Phase 14C — Python orchestration for the persistent Apps Script aggregator.

Wraps `apps_script.run_function(script_id, "cross_aggregate", ...)` with:
  - JSON parsing of clasp's loose stdout format
  - Resumable iteration: when the Apps Script side hits its 4.5-min safety
    budget, it returns {status: "incomplete", token, ...}. We re-invoke with
    that token until status=complete (capped at max_iterations).
  - Translation of the Apps Script response into the bulk-payload shape so
    cross_aggregate looks like sheets_bulk_metric to the agent.

One quota token consumed against the Apps Script Execution API regardless
of how many spreadsheets the script opens internally. That's the whole
point — at N=500 books, this is ~500× cheaper than direct Sheets reads.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

# Supported aggregation ops — must match SUPPORTED_OPS in apps_script_src/aggregator/Code.gs
SUPPORTED_OPS = {"sum", "avg", "min", "max", "count", "list"}

DEFAULT_MAX_ITERATIONS = 5


def parse_clasp_run_output(raw: str) -> dict:
    """Extract the JSON return value from `clasp run` stdout.

    clasp's output is loose — typically `Running <fn>...\\n{...JSON...}`
    but version-dependent. We try direct parse, then progressively narrow.
    """
    if not isinstance(raw, str):
        raise ValueError(f"expected string output, got {type(raw).__name__}")
    raw = raw.strip()
    if not raw:
        raise ValueError("clasp returned empty output")

    # Strategy 1: whole thing parses (clean output)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find a line that starts with `{` and parse from there to end
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            candidate = "\n".join(lines[i:]).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # Strategy 3: regex extract the largest {...} substring (last-resort)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"could not parse JSON from clasp output: {raw[:300]!r}")


def _to_bulk_payload(parsed: dict, n: int, started: float, iterations: int) -> dict:
    """Convert Apps Script's complete-response into the bulk-payload shape."""
    value = parsed.get("value")
    meta_in = parsed.get("_meta") or {}
    op = meta_in.get("op", "sum")
    per_file_count = parsed.get("per_file_count", 0)
    errors_count = parsed.get("errors_count", 0)
    errors = parsed.get("errors", [])

    # cross_aggregate doesn't return per-file values, so stats are minimal —
    # the aggregate is the answer. Surface it in both `value` (top-level)
    # AND the relevant stats field so existing payload-consumers keep working.
    stats = {
        "n_ok": per_file_count,
        "n_err": errors_count,
        "sum": value if op == "sum" else None,
        "mean": value if op == "avg" else None,
        "p50": None,
        "p95": None,
        "min": value if op == "min" else None,
        "max": value if op == "max" else None,
    }

    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    payload: dict = {
        "value": value,
        "stats": stats,
        "outliers": {"top": [], "bottom": []},  # server-side aggregation — no per-file ordering
        "errors": errors[:5],
        "_meta": {
            "result_token": None,  # no spill — server aggregated
            "n": n,
            "duration_ms": duration_ms,
            "op": op,
            "tool": "sheets_cross_aggregate",
            "iterations_used": iterations,
            "apps_script_duration_ms": meta_in.get("duration_ms"),
            "sheet": meta_in.get("sheet"),
            "cell": meta_in.get("cell"),
        },
    }
    if errors:
        payload["_meta"]["truncated"] = True
        payload["_meta"]["truncation_reason"] = f"{errors_count} per-file errors"
    return payload


def run_with_resumption(
    spreadsheet_ids: list[str],
    sheet: str,
    cell: str,
    op: str,
    script_id: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    account: str = "main",
) -> dict:
    """Invoke the Apps Script aggregator, iterating until status=complete.

    Returns the parsed bulk-payload dict. Raises RuntimeError if the run
    hits max_iterations without completing (e.g. for N>>500 with slow opens).

    Uses Apps Script Execution API (apps_script_api.run_function) — NOT clasp
    — because clasp's OAuth client is in a different GCP project from ours,
    leading to permission failures on our scripts. The API path uses the same
    OAuth credentials as the rest of the agent.
    """
    if not isinstance(spreadsheet_ids, list) or not spreadsheet_ids:
        raise ValueError("spreadsheet_ids must be a non-empty list")
    if not sheet or not cell:
        raise ValueError("sheet and cell are required")
    if op not in SUPPORTED_OPS:
        raise ValueError(f"unknown op {op!r}; expected one of {sorted(SUPPORTED_OPS)}")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    from src.tools import apps_script_api

    started = time.perf_counter()
    n = len(spreadsheet_ids)
    resume_token: str | None = None

    for iteration in range(1, max_iterations + 1):
        params: list[Any] = [spreadsheet_ids, sheet, cell, op]
        if resume_token:
            params.append(resume_token)

        api_resp = apps_script_api.run_function(
            script_id, "cross_aggregate",
            params=params, dev_mode=True, account=account,
        )
        if not api_resp.get("ok"):
            raise RuntimeError(
                f"Apps Script cross_aggregate failed: "
                f"{api_resp.get('error_message') or api_resp.get('error_type')}"
            )
        parsed = api_resp.get("result") or {}
        status = parsed.get("status")

        if status == "complete":
            return _to_bulk_payload(parsed, n, started, iteration)

        if status == "incomplete":
            resume_token = parsed.get("token")
            if not resume_token:
                raise RuntimeError(
                    f"Apps Script returned status=incomplete without token: {parsed}"
                )
            continue

        # status == "error" or anything else
        raise RuntimeError(
            f"Apps Script cross_aggregate returned unexpected status: {parsed}"
        )

    raise RuntimeError(
        f"cross_aggregate did not complete in {max_iterations} iterations "
        f"(last resume_token={resume_token!r}). Increase max_iterations or "
        f"reduce the input set."
    )


def fetch_status(token: str, script_id: str, account: str = "main") -> dict:
    """Peek at progress of an incomplete run without resuming."""
    from src.tools import apps_script_api
    api_resp = apps_script_api.run_function(
        script_id, "cross_aggregate_status",
        params=[token], dev_mode=True, account=account,
    )
    if not api_resp.get("ok"):
        return {"status": "error", "reason": api_resp.get("error_message")}
    return api_resp.get("result") or {}


# Default per-chunk size — large enough to amortize HTTP roundtrip (~10-15s
# overhead per Apps Script call), small enough that each chunk fits well
# under Google's L7 LB ~60s timeout window AND won't trigger retries.
# 100 books × ~500ms/openById ≈ 50s server time + ~10-15s HTTP ≈ ~70s wall.
DEFAULT_CHUNK_SIZE = 100
DEFAULT_MAX_CONCURRENT_CHUNKS = 5


def _merge_chunk_results(chunk_results: list[dict], op: str, n_total: int, started: float) -> dict:
    """Combine per-chunk bulk-payload dicts into a single result.

    Each chunk_result has shape from _to_bulk_payload: {value, stats, errors, _meta}.
    Aggregation rules:
      sum/count → sum across chunks
      avg/mean → weighted by per-chunk n_ok
      min → min across chunks; max → max across chunks
      list → concat
    """
    import time as _time

    n_ok = sum(r["stats"]["n_ok"] for r in chunk_results)
    n_err = sum(r["stats"]["n_err"] for r in chunk_results)
    all_errors = []
    for r in chunk_results:
        all_errors.extend(r.get("errors") or [])

    chunk_values = [r.get("value") for r in chunk_results
                    if r.get("value") is not None]

    if op in ("sum", "count"):
        total = sum(v for v in chunk_values if isinstance(v, (int, float))) if chunk_values else None
        merged_value = total
    elif op in ("avg", "mean"):
        # Weighted: sum(value_i * n_ok_i) / sum(n_ok_i)
        weighted = 0.0
        weight = 0
        for r in chunk_results:
            v = r.get("value")
            w = r["stats"]["n_ok"]
            if isinstance(v, (int, float)) and w > 0:
                weighted += v * w
                weight += w
        merged_value = (weighted / weight) if weight else None
    elif op == "min":
        nums = [v for v in chunk_values if isinstance(v, (int, float))]
        merged_value = min(nums) if nums else None
    elif op == "max":
        nums = [v for v in chunk_values if isinstance(v, (int, float))]
        merged_value = max(nums) if nums else None
    elif op == "list":
        merged_value = []
        for v in chunk_values:
            if isinstance(v, list):
                merged_value.extend(v)
    else:
        merged_value = chunk_values

    apps_script_durations = [r["_meta"].get("apps_script_duration_ms") for r in chunk_results
                             if r["_meta"].get("apps_script_duration_ms") is not None]

    stats = {
        "n_ok": n_ok,
        "n_err": n_err,
        "sum": merged_value if op == "sum" else None,
        "mean": merged_value if op in ("avg", "mean") else None,
        "p50": None,
        "p95": None,
        "min": merged_value if op == "min" else None,
        "max": merged_value if op == "max" else None,
    }

    meta = {
        "result_token": None,
        "n": n_total,
        "duration_ms": round((_time.perf_counter() - started) * 1000, 1),
        "op": op,
        "tool": "sheets_cross_aggregate",
        "chunked": True,
        "chunks_used": len(chunk_results),
        "apps_script_max_duration_ms": max(apps_script_durations) if apps_script_durations else None,
        "apps_script_sum_duration_ms": sum(apps_script_durations) if apps_script_durations else None,
    }
    if all_errors:
        meta["truncated"] = True
        meta["truncation_reason"] = f"{n_err} per-file errors across chunks"

    return {
        "value": merged_value,
        "stats": stats,
        "outliers": {"top": [], "bottom": []},
        "errors": all_errors[:5],
        "_meta": meta,
    }


def run_chunked_parallel(
    spreadsheet_ids: list[str],
    sheet: str,
    cell: str,
    op: str,
    script_id: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_CHUNKS,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    account: str = "main",
) -> dict:
    """Split ids into chunks of `chunk_size`, run each via run_with_resumption
    in parallel via ThreadPoolExecutor(max_concurrent), merge results.

    Why: single Apps Script call for 500 books takes ~3 min server-side, but
    Google's L7 LB drops the upstream connection around 60s, triggering
    RetryingHttpRequest retries. Each retry re-executes the script, ballooning
    wall-clock to ~30 min. Chunks of 100 books fit under that window and
    parallelism keeps total time at ~max(per_chunk) instead of sum.
    """
    if not isinstance(spreadsheet_ids, list) or not spreadsheet_ids:
        raise ValueError("spreadsheet_ids must be a non-empty list")
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")

    n = len(spreadsheet_ids)
    if n <= chunk_size:
        # Small enough — one shot, no chunking overhead
        return run_with_resumption(
            spreadsheet_ids, sheet, cell, op,
            script_id=script_id, max_iterations=max_iterations, account=account,
        )

    started = time.perf_counter()
    chunks = [spreadsheet_ids[i:i + chunk_size] for i in range(0, n, chunk_size)]

    from concurrent.futures import ThreadPoolExecutor
    workers = min(max_concurrent, len(chunks))
    chunk_results: list[dict | None] = [None] * len(chunks)
    errors_per_chunk: dict[int, Exception] = {}

    def _run_one(idx: int):
        try:
            r = run_with_resumption(
                chunks[idx], sheet, cell, op,
                script_id=script_id, max_iterations=max_iterations, account=account,
            )
            return idx, r, None
        except Exception as e:
            return idx, None, e

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xagg") as pool:
        futures = [pool.submit(_run_one, i) for i in range(len(chunks))]
        for fut in futures:
            idx, result, exc = fut.result()
            if exc is not None:
                errors_per_chunk[idx] = exc
            else:
                chunk_results[idx] = result

    if errors_per_chunk and not any(r is not None for r in chunk_results):
        # ALL chunks failed — surface the first error
        first_err = next(iter(errors_per_chunk.values()))
        raise RuntimeError(f"All chunks failed; first: {type(first_err).__name__}: {first_err}")

    # Drop None entries, log partial failure in meta
    successful = [r for r in chunk_results if r is not None]
    merged = _merge_chunk_results(successful, op, n_total=n, started=started)
    if errors_per_chunk:
        merged["_meta"]["chunk_failures"] = len(errors_per_chunk)
        merged["_meta"]["truncated"] = True
        merged["_meta"]["truncation_reason"] = (
            f"{len(errors_per_chunk)} of {len(chunks)} chunks failed"
        )
    return merged
