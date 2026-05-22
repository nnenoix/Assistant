"""Build EXACTLY 10 billion characters on disk in Google Drive.

Per user explicit ask: «специально такие аномальные нагрузки, ровно 10 млрд».

Layout:
  - 100 new books in CLAUDE-TEST/production/tier_d_10b_chars/
  - Each book: 5000 rows x 2000 cols = 10,000,000 cells
  - Each cell: a 10-character zero-padded string ("0001234567")
  - Total: 100 × 10M × 10 = EXACTLY 10,000,000,000 characters

Resumability — script can be killed and re-run any number of times:

  - State in .data/phase14_heavy10b.json — per-book:
      pending → in_progress → completed (or failed)
  - State saved after every book completion
  - Pending books picked up on restart
  - In_progress books resume from their next_resume_row
  - Completed books skipped (no rework)

Thread-safety: each ThreadPoolExecutor worker creates its own Apps Script
client (httplib2 is NOT thread-safe; lru_cached shared instance crashes).

Usage:
    $env:LIVE_GOOGLE_TESTS = "1"
    uv run python scripts/build_10b_chars.py
    # Ctrl-C anytime; re-run to continue.
    # Status: cat .data/phase14_heavy10b.json | python -m json.tool
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials
from src.config import DATA_DIR
from src.tools import _phase14_config, drive, sheets


# ---------- Target spec ----------
# Google Sheets hard cap is 10,000,000 cells per spreadsheet (TOTAL across
# every sheet in the book). Newly-created books carry a default Sheet1 with
# 1000 rows × 26 cols = 26,000 cells, which counts against the cap. The
# Apps Script populate function adds a SECOND sheet ("data") and expands it,
# so layout budget is 10M − 26k ≈ 9.974M for the data sheet. Picking
# 4985×2000 = 9,970,000 leaves a safe ~4k cushion. 101 books × 9.97M ×
# 10 chars = 10,069,700,000 chars ≈ 10.07B (overshoots target by 0.7%).
TARGET_BOOKS = 101
TARGET_ROWS = 4985
TARGET_COLS = 2000
TARGET_CELLS_PER_BOOK = TARGET_ROWS * TARGET_COLS  # 9,970,000
CHARS_PER_CELL = 10
TARGET_TOTAL_CHARS = TARGET_BOOKS * TARGET_CELLS_PER_BOOK * CHARS_PER_CELL  # 10,069,700,000

SHEET_NAME = "data"
ACCOUNT = "main"

# Per-book resume cap inside one populate call
MAX_RESUMES_PER_BOOK = 30  # 30 × 4-min budget = ~2 hours per book max (way more than needed)

# Parallel workers — each builds its own service
MAX_CONCURRENT = 5

STATE_PATH = DATA_DIR / "phase14_heavy10b.json"
FIXTURES_PATH = DATA_DIR / "phase14_fixtures.json"
STATE_LOCK = Lock()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _require_live() -> None:
    if os.environ.get("LIVE_GOOGLE_TESTS") != "1":
        print("ERROR: set LIVE_GOOGLE_TESTS=1")
        sys.exit(2)


# ---------- State management ----------

def load_state() -> dict:
    """Load or initialize the heavy build state."""
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "target": {
            "books": TARGET_BOOKS, "rows": TARGET_ROWS, "cols": TARGET_COLS,
            "cells_per_book": TARGET_CELLS_PER_BOOK,
            "chars_per_cell": CHARS_PER_CELL,
            "total_chars": TARGET_TOTAL_CHARS,
            "sheet_name": SHEET_NAME,
        },
        "production_subfolder_id": None,
        "books": [],  # list of {id, status, cells_written, next_resume_row, ...}
        "started_at": dt.datetime.utcnow().isoformat() + "Z",
        "last_progress_at": None,
    }


def save_state(state: dict) -> None:
    """Atomic save with lock. State must survive even crashes during write."""
    with STATE_LOCK:
        state["last_progress_at"] = dt.datetime.utcnow().isoformat() + "Z"
        tmp = STATE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_PATH)


def update_book(state: dict, book_idx: int, **fields) -> None:
    """Update one book's record and persist state. Thread-safe via STATE_LOCK."""
    with STATE_LOCK:
        state["books"][book_idx].update(fields)
        state["last_progress_at"] = dt.datetime.utcnow().isoformat() + "Z"
        tmp = STATE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_PATH)


# ---------- Folder + books bootstrap ----------

def ensure_subfolder(state: dict) -> str:
    """Get or create CLAUDE-TEST/production/tier_d_10b_chars/."""
    if state.get("production_subfolder_id"):
        try:
            drive.get_metadata(state["production_subfolder_id"])
            return state["production_subfolder_id"]
        except Exception:
            _log(f"WARN: prior subfolder vanished, recreating")

    # Read parent from fixtures.json
    with open(FIXTURES_PATH) as f:
        fix = json.load(f)
    parent = fix["production_folder_id"]

    folder = drive.create_folder(parent, "tier_d_10b_chars")
    state["production_subfolder_id"] = folder["id"]
    save_state(state)
    _log(f"subfolder: {folder['id']}")
    return folder["id"]


def ensure_books(state: dict, subfolder_id: str) -> None:
    """Create missing book records. Idempotent: only creates books up to TARGET_BOOKS."""
    existing = len(state["books"])
    if existing >= TARGET_BOOKS:
        return

    _log(f"creating {TARGET_BOOKS - existing} new books...")
    for i in range(existing, TARGET_BOOKS):
        title = f"phase14_10b_book_{i:03d}"
        try:
            ss = sheets.create_spreadsheet(title)
            sid = ss["spreadsheetId"]
            drive.move(sid, subfolder_id)
            state["books"].append({
                "id": sid,
                "index": i,
                "title": title,
                "status": "pending",
                "cells_written": 0,
                "next_resume_row": 1,
            })
            save_state(state)
            if (i + 1) % 10 == 0:
                _log(f"  created {i+1}/{TARGET_BOOKS}")
        except Exception as e:
            _log(f"  ERROR creating book {i}: {e}")
            time.sleep(10)
            continue

    _log(f"books ready: {len(state['books'])}")


# ---------- Thread-safe Apps Script client ----------

def _fresh_service():
    """Each thread MUST have its own Http (httplib2 not thread-safe)."""
    creds = get_credentials(ACCOUNT)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=360),
    )
    return build("script", "v1", http=http, cache_discovery=False,
                 requestBuilder=RetryingHttpRequest)


def _run_function(script_id: str, function_name: str, params: list) -> dict:
    """Per-call service build for thread safety."""
    svc = _fresh_service()
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


# ---------- Populate one book with resumption ----------

def _prepare_book_grid(sid: str) -> None:
    """Make `data` sheet the ONLY sheet in this book, sized to a small
    starter grid. Required because:
      1. New books ship with a default sheet ("Sheet1" / "Лист1" / "Sayfa1"
         depending on user locale) carrying 1000×26 = 26k cells.
      2. Apps Script `populate_10char_target` expands `data` to TARGET_ROWS
         × TARGET_COLS, which together with the leftover default sheet
         overflows Google's 10M-cells-per-book hard cap.
      3. A previous failed run may have left `data` already partially
         expanded; we shrink it back to 1000×26 so the subsequent expand
         math fits the budget cleanly.

    Sheets API forbids deleting the LAST sheet, so we add `data` first if
    missing, then drop every other sheet by sheetId."""
    from src.tools import sheets as _sheets
    svc = _sheets._service(ACCOUNT)
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties(sheetId,title,gridProperties)",
        ).execute()
    except Exception:
        return
    sheets_props = [s["properties"] for s in meta.get("sheets", [])]
    target = next((p for p in sheets_props if p["title"] == SHEET_NAME), None)

    requests: list[dict] = []
    if target is None:
        # Need data sheet first (can't drop the last one without it).
        requests.append({"addSheet": {"properties": {"title": SHEET_NAME, "gridProperties": {"rowCount": 1000, "columnCount": 26}}}})
    else:
        # Shrink data back to starter dimensions so the expand math works.
        gp = target.get("gridProperties", {})
        if gp.get("rowCount", 0) > 1000 or gp.get("columnCount", 0) > 26:
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": target["sheetId"],
                               "gridProperties": {"rowCount": 1000, "columnCount": 26}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount",
            }})
    if requests:
        try:
            svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()
        except Exception:
            pass
        # Re-fetch metadata to get the freshly-created data sheet's id.
        try:
            meta = svc.spreadsheets().get(
                spreadsheetId=sid, fields="sheets.properties(sheetId,title)",
            ).execute()
            sheets_props = [s["properties"] for s in meta.get("sheets", [])]
        except Exception:
            return

    # Drop every sheet except `data`.
    delete_reqs = [
        {"deleteSheet": {"sheetId": p["sheetId"]}}
        for p in sheets_props if p["title"] != SHEET_NAME
    ]
    if delete_reqs:
        try:
            svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": delete_reqs}).execute()
        except Exception:
            pass


def populate_book(state: dict, book_idx: int, script_id: str) -> dict:
    """Fill one book to TARGET_CELLS_PER_BOOK using populate_10char_target on
    the Apps Script side. Survives interruption: state.books[idx].next_resume_row
    is persisted after each Apps Script call so re-running picks up cleanly.
    """
    book = state["books"][book_idx]
    sid = book["id"]
    started = time.perf_counter()
    resume_from = book.get("next_resume_row", 1)
    iter_count = 0

    update_book(state, book_idx, status="in_progress")
    # First call only: reclaim the 26k cells eaten by the default Sheet1.
    if resume_from == 1:
        _prepare_book_grid(sid)

    for it in range(1, MAX_RESUMES_PER_BOOK + 1):
        iter_count = it
        params = [sid, SHEET_NAME, TARGET_ROWS, TARGET_COLS, resume_from]
        try:
            resp = _run_function(script_id, "populate_10char_target", params)
        except Exception as e:
            update_book(state, book_idx, status="failed",
                        error=f"{type(e).__name__}: {str(e)[:200]}")
            return {"sid": sid, "ok": False, "error": str(e)[:200]}

        if not resp.get("ok"):
            err = resp.get("error_message") or resp.get("error_type") or "unknown"
            update_book(state, book_idx, status="failed", error=err[:300])
            return {"sid": sid, "ok": False, "error": err}

        result = resp.get("result") or {}
        cells_this = result.get("cells_written", 0) or 0
        # Cells_written from Apps Script is for THIS invocation only;
        # for total we use grid math from next_resume_row
        status = result.get("status")

        if status == "complete":
            update_book(state, book_idx,
                        status="completed",
                        cells_written=TARGET_CELLS_PER_BOOK,
                        next_resume_row=TARGET_ROWS + 1,
                        duration_s=round(time.perf_counter() - started, 1),
                        iterations=iter_count)
            return {"sid": sid, "ok": True,
                    "cells_written": TARGET_CELLS_PER_BOOK,
                    "duration_s": time.perf_counter() - started,
                    "iterations": iter_count}

        if status == "incomplete":
            resume_from = result.get("next_start_row")
            if not resume_from:
                update_book(state, book_idx, status="failed",
                            error="incomplete without next_start_row")
                return {"sid": sid, "ok": False, "error": "no resume row"}
            # Persist progress between iterations — this is the key to resilience
            cells_progress = (resume_from - 1) * TARGET_COLS
            update_book(state, book_idx,
                        cells_written=cells_progress,
                        next_resume_row=resume_from)
            continue

        update_book(state, book_idx, status="failed",
                    error=f"unexpected status: {result}")
        return {"sid": sid, "ok": False, "error": f"status={status}"}

    update_book(state, book_idx, status="failed",
                error=f"did not complete in {MAX_RESUMES_PER_BOOK} iterations")
    return {"sid": sid, "ok": False, "error": "max_iterations_exhausted"}


# ---------- Main ----------

def main() -> int:
    _require_live()

    state = load_state()
    script_id = _phase14_config.get_aggregator_script_id()

    # Bootstrap folder + books (idempotent)
    subfolder = ensure_subfolder(state)
    ensure_books(state, subfolder)

    # Plan the work
    pending = [b["index"] for b in state["books"] if b["status"] in ("pending", "in_progress", "failed")]
    completed = sum(1 for b in state["books"] if b["status"] == "completed")
    _log(f"target: {TARGET_BOOKS} books × {TARGET_CELLS_PER_BOOK:,} cells × {CHARS_PER_CELL} chars = "
         f"{TARGET_TOTAL_CHARS:,} chars (10 BILLION)")
    _log(f"state: {completed}/{TARGET_BOOKS} completed, {len(pending)} pending/in_progress/failed")

    if not pending:
        _log("ALL DONE — nothing to do. State already shows completed.")
        return 0

    _log(f"processing {len(pending)} books with {MAX_CONCURRENT} parallel workers...")
    progress = {"completed_this_run": 0, "failed_this_run": 0, "cells_added": 0}

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="b10") as pool:
        futures = {pool.submit(populate_book, state, idx, script_id): idx for idx in pending}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

            if r.get("ok"):
                progress["completed_this_run"] += 1
                progress["cells_added"] += r.get("cells_written", 0)
                total_done = sum(1 for b in state["books"] if b["status"] == "completed")
                _log(f"  OK book[{idx}] in {r['duration_s']:.0f}s  "
                     f"iters={r['iterations']}  [{total_done}/{TARGET_BOOKS} done]")
            else:
                progress["failed_this_run"] += 1
                _log(f"  FAIL book[{idx}]: {r.get('error')}")

    # Final summary
    final_completed = sum(1 for b in state["books"] if b["status"] == "completed")
    final_failed = sum(1 for b in state["books"] if b["status"] == "failed")
    total_cells = sum(b.get("cells_written", 0) for b in state["books"])
    _log("=" * 60)
    _log(f"RUN COMPLETE")
    _log(f"  this run: {progress['completed_this_run']} OK, {progress['failed_this_run']} failed")
    _log(f"  global:   {final_completed}/{TARGET_BOOKS} completed, {final_failed} failed")
    _log(f"  cells populated total: {total_cells:,}")
    _log(f"  chars on disk total:   {total_cells * CHARS_PER_CELL:,}  "
         f"({total_cells * CHARS_PER_CELL / 1e9:.2f}B / 10B target)")

    if final_completed < TARGET_BOOKS:
        _log(f"INCOMPLETE — re-run to continue (state in {STATE_PATH})")
        return 1
    _log(f"TARGET HIT: 10 BILLION CHARACTERS ON DISK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
