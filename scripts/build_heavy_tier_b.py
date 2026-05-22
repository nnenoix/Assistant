"""Build Tier B HEAVY: 50 books x ~4M populated cells each ≈ 2 BILLION chars total.

User pushback: original Tier B (50 x 70k cells) is only ~40M chars total.
For a realistic production-scale stress, each book needs to be much heavier.

This script uses the persistent Apps Script `populate_heavy_book` function
(deployed as part of ChatAgentAggregator) to fill each Tier B book server-side
via setValues — ~5x faster than Python write_range.

Strategy:
  - Target per book: 4000 rows x 1000 cols = 4M cells (~40M chars at 10 chars/cell)
  - Apps Script runs ~30 chunks of 1000 rows x 1000 cols = 1M cells each
  - Each book takes ~2-4 minutes server-side
  - 50 books processed K=5 in parallel via ThreadPoolExecutor → ~30-60 min total wall-clock
  - Resumable: if a single book invocation hits 4-min internal budget,
    we re-invoke with resumeFromRow until complete

Usage:
    $env:LIVE_GOOGLE_TESTS = "1"
    uv run python scripts/build_heavy_tier_b.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials
from src.config import DATA_DIR
from src.tools import _phase14_config

FIXTURES_PATH = DATA_DIR / "phase14_fixtures.json"

# Per-book target. Sheets max grid is 10M cells. 4000x1000=4M leaves headroom
# AND keeps per-book wall-clock at ~3-4 min via Apps Script setValues.
TARGET_ROWS = 4000
TARGET_COLS = 1000
SHEET_NAME = "Год факт"

# Parallelism: 5 concurrent Apps Script invocations. Each runs ~3-4 min;
# Google's per-user concurrent Apps Script execution limit is ~30, so 5 is safe.
MAX_CONCURRENT = 5

# Resumption cap per book — should be 1 in normal cases (4 min < script budget)
MAX_RESUMES_PER_BOOK = 5


def _log(msg: str, *, file=None) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if file is not None:
        file.write(line + "\n")
        file.flush()


def _require_live() -> None:
    if os.environ.get("LIVE_GOOGLE_TESTS") != "1":
        print("ERROR: set LIVE_GOOGLE_TESTS=1")
        sys.exit(2)


def _fresh_service(account: str):
    """Build a FRESH Apps Script API client. Each thread MUST have its own
    httplib2.Http because httplib2 is not thread-safe (shared instance =
    deadlocks or silent crashes on Windows).
    """
    creds = get_credentials(account)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=360),
    )
    return build("script", "v1", http=http, cache_discovery=False,
                 requestBuilder=RetryingHttpRequest)


def _run_function_thread_safe(script_id: str, function_name: str, params: list, account: str) -> dict:
    """Like apps_script_api.run_function but builds a per-call service so
    threads don't share state. Returns normalized {ok, result, error...}.
    """
    svc = _fresh_service(account)
    body = {"function": function_name, "devMode": True, "parameters": params}
    resp = svc.scripts().run(scriptId=script_id, body=body).execute()
    if "error" in resp:
        err = resp["error"]
        details = err.get("details") or [{}]
        d0 = details[0] if details else {}
        return {"ok": False,
                "error_type": d0.get("errorType") or err.get("status"),
                "error_message": d0.get("errorMessage") or err.get("message")}
    return {"ok": True, "result": (resp.get("response") or {}).get("result")}


def populate_one(sid: str, script_id: str, account: str = "main") -> dict:
    """Populate one book to TARGET_ROWS x TARGET_COLS. Resumes if needed.
    Thread-safe: uses _run_function_thread_safe which builds a fresh service."""
    started = time.perf_counter()
    resume_from = 1
    total_cells = 0
    iterations = 0

    for it in range(1, MAX_RESUMES_PER_BOOK + 1):
        iterations = it
        params = [sid, SHEET_NAME, TARGET_ROWS, TARGET_COLS, resume_from]
        resp = _run_function_thread_safe(script_id, "populate_heavy_book", params, account)
        if not resp.get("ok"):
            return {
                "sid": sid, "ok": False,
                "error": resp.get("error_message") or resp.get("error_type"),
                "iterations": iterations,
                "duration_s": time.perf_counter() - started,
            }
        result = resp.get("result") or {}
        cells_this = result.get("cells_written", 0) or 0
        total_cells += cells_this
        status = result.get("status")

        if status == "complete":
            return {
                "sid": sid, "ok": True,
                "total_cells_written": total_cells,
                "iterations": iterations,
                "duration_s": time.perf_counter() - started,
                "apps_script_duration_ms": result.get("duration_ms"),
            }
        if status == "incomplete":
            resume_from = result.get("next_start_row")
            if not resume_from:
                return {"sid": sid, "ok": False, "error": "incomplete without next_start_row",
                        "iterations": iterations}
            continue
        return {"sid": sid, "ok": False, "error": f"unexpected status: {result}",
                "iterations": iterations}

    return {"sid": sid, "ok": False, "error": f"did not complete in {MAX_RESUMES_PER_BOOK} resumes"}


def main() -> int:
    _require_live()

    with open(FIXTURES_PATH) as f:
        fix = json.load(f)
    tier_b_ids = fix.get("tier_b") or []
    if not tier_b_ids:
        print("ERROR: tier_b empty in fixtures. Build base first.")
        return 2

    # Allow capping via env: HEAVY_LIMIT=5 → process first 5 books only
    limit_env = os.environ.get("HEAVY_LIMIT")
    if limit_env:
        tier_b_ids = tier_b_ids[:int(limit_env)]
        _log(f"HEAVY_LIMIT={limit_env}, capping to {len(tier_b_ids)} books")

    script_id = _phase14_config.get_aggregator_script_id()
    _log(f"Heavy population: {len(tier_b_ids)} books x {TARGET_ROWS}x{TARGET_COLS} = "
         f"{TARGET_ROWS * TARGET_COLS:,} cells/book → total {len(tier_b_ids) * TARGET_ROWS * TARGET_COLS:,} cells")
    _log(f"Concurrency: {MAX_CONCURRENT}, script_id={script_id[:12]}...")

    overall_started = time.perf_counter()
    results: list[dict] = []
    log_path = DATA_DIR / "sweep_results" / f"heavy_b_{time.strftime('%Y-%m-%dT%H-%M-%S')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    progress = {"total_cells": 0, "completed": 0, "errors": 0}

    with log_path.open("w", encoding="utf-8") as logf:
        _log(f"log: {log_path}", file=logf)

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="heavyB") as pool:
            futures = {pool.submit(populate_one, sid, script_id): sid for sid in tier_b_ids}
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"sid": sid, "ok": False, "error": f"{type(e).__name__}: {e}"}
                results.append(r)
                if r.get("ok"):
                    progress["completed"] += 1
                    progress["total_cells"] += r.get("total_cells_written", 0) or 0
                    _log(f"  OK {sid[:12]}... in {r['duration_s']:.0f}s  "
                         f"cells={r.get('total_cells_written'):,}  iters={r['iterations']}  "
                         f"[{progress['completed']}/{len(tier_b_ids)}]",
                         file=logf)
                else:
                    progress["errors"] += 1
                    _log(f"  FAIL {sid[:12]}... ERROR: {r.get('error')}", file=logf)

        total = time.perf_counter() - overall_started
        _log("", file=logf)
        _log(f"DONE in {total/60:.1f} min", file=logf)
        _log(f"  OK completed: {progress['completed']}/{len(tier_b_ids)}", file=logf)
        _log(f"  FAIL errors:  {progress['errors']}", file=logf)
        _log(f"  total cells populated: {progress['total_cells']:,}", file=logf)
        _log(f"  estimated total chars: ~{progress['total_cells'] * 10:,}", file=logf)

    # Mark fixtures.json with heavy timestamp
    fix["tier_b_heavy_populated_at"] = time.strftime("%Y-%m-%dT%H-%M-%S")
    fix["tier_b_heavy_target_cells_per_book"] = TARGET_ROWS * TARGET_COLS
    fix["tier_b_heavy_total_cells"] = progress["total_cells"]
    with open(FIXTURES_PATH, "w") as f:
        json.dump(fix, f, indent=2, ensure_ascii=False)

    return 0 if progress["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
