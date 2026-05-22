"""Phase 14H — Build production-scale fixtures (one-time, idempotent).

Three tiers, all under CLAUDE-TEST/production/<timestamp>/:

  TIER A: 500 trivial books — Drive search/list/quota + chunked bulk tests
  TIER B: 50 books × ~70k cells — realistic per-book perf + cross_aggregate
  TIER C: 1 book × ~700k cells — single-heavy reads from a 35M-char-ish book

Total build cost: ~2-3 hours wall-clock. Run overnight via:

    $env:LIVE_GOOGLE_TESTS = "1"
    uv run python scripts/build_phase14_fixtures.py

Idempotent: rerunning skips any tier whose IDs in .data/phase14_fixtures.json
still resolve via drive.get_metadata. Crash mid-build → next run picks up.

Per CLAUDE.md: NEVER auto-cleans. Reusable across many stress runs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.tools import drive, sheets

FIXTURES_PATH = DATA_DIR / "phase14_fixtures.json"
INTEGRATION_CONFIG_PATH = DATA_DIR / "integration_test_config.json"

# Tier sizes — match the approved Phase 14 plan. Adjust here to scale up/down.
TIER_A_COUNT = 500
TIER_B_COUNT = 50
TIER_B_ROWS = 500   # 500 rows × 140 cols ≈ 70k cells per book
TIER_B_COLS = 140
TIER_C_ROWS = 5000  # 5000 × 140 = 700k cells ≈ 35M chars-ish
TIER_C_COLS = 140

# Pacing — keeps us under Drive's 20-mutation-per-minute soft cap
PACE_BETWEEN_CREATES_S = 0.3

# Named ranges used by Tier A and Tier B for sheets_bulk_metric tests
NAMED_RANGES = {
    "Vyruchka":      ("Год факт", "B10"),
    "Marzha":        ("Год факт", "B30"),
    "ChistayaPribyl": ("Год факт", "B45"),
}


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _require_live() -> None:
    if os.environ.get("LIVE_GOOGLE_TESTS") != "1":
        print("ERROR: set LIVE_GOOGLE_TESTS=1 to run against live Google API")
        sys.exit(2)


def _load_fixtures() -> dict:
    if FIXTURES_PATH.exists():
        return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return {}


def _save_fixtures(data: dict) -> None:
    data["_updated"] = dt.datetime.utcnow().isoformat() + "Z"
    FIXTURES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _claude_test_root() -> str:
    if not INTEGRATION_CONFIG_PATH.exists():
        print(f"ERROR: {INTEGRATION_CONFIG_PATH} missing. Run "
              "scripts/seed_claude_test.py --bootstrap-only first.")
        sys.exit(2)
    cfg = json.loads(INTEGRATION_CONFIG_PATH.read_text(encoding="utf-8"))
    fid = cfg.get("claude_test_folder_id")
    if not fid:
        print("ERROR: claude_test_folder_id missing from integration config")
        sys.exit(2)
    return fid


def _ensure_production_folder(fixtures: dict, root_id: str) -> str:
    """Get or create CLAUDE-TEST/production/<timestamp>/."""
    if fid := fixtures.get("production_folder_id"):
        # Verify it still exists
        try:
            drive.get_metadata(fid)
            return fid
        except Exception:
            _log(f"WARN: production folder {fid} disappeared; will create new one")
    # Find-or-create the "production" subfolder under CLAUDE-TEST
    # (search first to reuse if there's an existing one for incremental builds)
    res = drive.search(name_contains="production", page_size=10)
    for f in res.get("files", []):
        # Match parent == CLAUDE-TEST root
        # (drive.search doesn't filter by parent; we filter here)
        if f.get("name") == "production":
            fixtures["production_folder_id"] = f["id"]
            _save_fixtures(fixtures)
            return f["id"]
    folder = drive.create_folder(root_id, "production")
    fixtures["production_folder_id"] = folder["id"]
    _save_fixtures(fixtures)
    return folder["id"]


def _book_alive(spreadsheet_id: str) -> bool:
    try:
        sheets.get_metadata(spreadsheet_id)
        return True
    except Exception:
        return False


# ============================================================================
# TIER A — 500 trivial books with named ranges
# ============================================================================

def build_tier_a(fixtures: dict, production_id: str) -> list[str]:
    """500 books × ~200 cells each. Each has named ranges Vyruchka/Marzha/ChistayaPribyl
    pointing to known cells so sheets_bulk_metric tests can read them all."""
    existing = fixtures.get("tier_a") or []
    alive = [sid for sid in existing if _book_alive(sid)]
    if len(alive) == len(existing) == TIER_A_COUNT:
        _log(f"TIER A already built ({TIER_A_COUNT} books) — skipping")
        return existing

    if alive:
        _log(f"TIER A partially built ({len(alive)}/{TIER_A_COUNT}) — resuming")

    # Subfolder for Tier A
    folder_id = fixtures.get("tier_a_folder")
    if not folder_id or not _book_alive(folder_id):  # _book_alive works for folders too
        try:
            f = drive.create_folder(production_id, "tier_a_500_trivial")
            folder_id = f["id"]
            fixtures["tier_a_folder"] = folder_id
            _save_fixtures(fixtures)
        except Exception as e:
            _log(f"ERROR creating Tier A folder: {e}")
            raise

    ids = list(alive)
    started = time.perf_counter()

    while len(ids) < TIER_A_COUNT:
        i = len(ids)
        title = f"phase14_tierA_{i:03d}"
        try:
            ss = sheets.create_spreadsheet(title)
            sid = ss["spreadsheetId"]
            drive.move(sid, folder_id)

            # Write 200 cells of fake P&L scaffolding
            rng = "Год факт!A1:B100"
            try:
                sheets.add_sheet(sid, "Год факт")
            except Exception:
                pass  # default sheet may already be named differently — skip

            rows = []
            for r in range(100):
                rows.append([f"Метрика {r}", float(1000 + r * 13)])
            sheets.write_range(sid, "Год факт!A1:B100", rows)

            # Named ranges → known cells (so bulk_metric can read them by cell)
            for name, (sheet_name, cell) in NAMED_RANGES.items():
                try:
                    sheets.create_named_range(sid, name, f"{sheet_name}!{cell}")
                except Exception as e:
                    _log(f"WARN: named_range {name} on {sid} failed: {e}")

            ids.append(sid)
            fixtures["tier_a"] = ids
            _save_fixtures(fixtures)

            if (i + 1) % 25 == 0:
                elapsed = time.perf_counter() - started
                rate = (i + 1 - len(alive)) / elapsed if elapsed else 0
                eta = (TIER_A_COUNT - i - 1) / rate if rate else 0
                _log(f"TIER A: {i+1}/{TIER_A_COUNT}  rate={rate:.2f}/s  eta={eta/60:.1f}min")

            time.sleep(PACE_BETWEEN_CREATES_S)
        except Exception as e:
            _log(f"ERROR creating Tier A book {i}: {e} — retrying in 30s")
            time.sleep(30)

    total = time.perf_counter() - started
    _log(f"TIER A complete: {len(ids)} books in {total/60:.1f}min")
    return ids


# ============================================================================
# TIER B — 50 books × ~70k cells each (realistic perf)
# ============================================================================

def build_tier_b(fixtures: dict, production_id: str) -> list[str]:
    """50 books with 500 rows × 140 cols = ~70k cells each."""
    existing = fixtures.get("tier_b") or []
    alive = [sid for sid in existing if _book_alive(sid)]
    if len(alive) == len(existing) == TIER_B_COUNT:
        _log(f"TIER B already built ({TIER_B_COUNT} books) — skipping")
        return existing

    if alive:
        _log(f"TIER B partially built ({len(alive)}/{TIER_B_COUNT}) — resuming")

    folder_id = fixtures.get("tier_b_folder")
    if not folder_id or not _book_alive(folder_id):
        f = drive.create_folder(production_id, "tier_b_50_realistic")
        folder_id = f["id"]
        fixtures["tier_b_folder"] = folder_id
        _save_fixtures(fixtures)

    ids = list(alive)
    started = time.perf_counter()

    while len(ids) < TIER_B_COUNT:
        i = len(ids)
        title = f"phase14_tierB_{i:02d}_700k"
        try:
            ss = sheets.create_spreadsheet(title)
            sid = ss["spreadsheetId"]
            drive.move(sid, folder_id)
            try:
                sheets.add_sheet(sid, "Год факт")
            except Exception:
                pass

            # Fill 500 rows × 140 cols in chunks to avoid 10MB request limit
            CHUNK = 100  # rows per write
            for chunk_start in range(0, TIER_B_ROWS, CHUNK):
                chunk_rows = []
                for r in range(chunk_start, min(chunk_start + CHUNK, TIER_B_ROWS)):
                    row = [f"Row_{r}"] + [float((r * 7 + c * 13) % 1_000_000) for c in range(TIER_B_COLS - 1)]
                    chunk_rows.append(row)
                last_col = sheets._col_to_a1(TIER_B_COLS - 1)
                rng = f"Год факт!A{chunk_start + 1}:{last_col}{chunk_start + len(chunk_rows)}"
                sheets.write_range(sid, rng, chunk_rows)

            # Named ranges
            for name, (sheet_name, cell) in NAMED_RANGES.items():
                try:
                    sheets.create_named_range(sid, name, f"{sheet_name}!{cell}")
                except Exception as e:
                    _log(f"WARN: named_range {name} on {sid}: {e}")

            ids.append(sid)
            fixtures["tier_b"] = ids
            _save_fixtures(fixtures)

            elapsed = time.perf_counter() - started
            rate = (i + 1 - len(alive)) / elapsed if elapsed else 0
            eta = (TIER_B_COUNT - i - 1) / rate if rate else 0
            _log(f"TIER B: {i+1}/{TIER_B_COUNT}  rate={rate*60:.1f}/min  eta={eta/60:.1f}min")
            time.sleep(PACE_BETWEEN_CREATES_S)
        except Exception as e:
            _log(f"ERROR creating Tier B book {i}: {e} — retrying in 60s")
            time.sleep(60)

    total = time.perf_counter() - started
    _log(f"TIER B complete: {len(ids)} books in {total/60:.1f}min")
    return ids


# ============================================================================
# TIER C — 1 heavy book × 700k cells
# ============================================================================

def build_tier_c(fixtures: dict, production_id: str) -> str:
    existing = fixtures.get("tier_c")
    if existing and _book_alive(existing):
        _log("TIER C already built — skipping")
        return existing

    started = time.perf_counter()
    ss = sheets.create_spreadsheet("phase14_tierC_heavy_35M")
    sid = ss["spreadsheetId"]
    drive.move(sid, production_id)
    try:
        sheets.add_sheet(sid, "Год факт")
    except Exception:
        pass

    # 5000 rows × 140 cols = 700k cells. Chunk at 250 rows/write (~35k cells per call)
    CHUNK = 250
    for chunk_start in range(0, TIER_C_ROWS, CHUNK):
        chunk_rows = []
        for r in range(chunk_start, min(chunk_start + CHUNK, TIER_C_ROWS)):
            row = [f"Row_{r}"] + [float((r * 7 + c * 13) % 1_000_000) for c in range(TIER_C_COLS - 1)]
            chunk_rows.append(row)
        last_col = sheets._col_to_a1(TIER_C_COLS - 1)
        rng = f"Год факт!A{chunk_start + 1}:{last_col}{chunk_start + len(chunk_rows)}"
        sheets.write_range(sid, rng, chunk_rows)
        if (chunk_start + CHUNK) % 1000 == 0:
            elapsed = time.perf_counter() - started
            _log(f"TIER C: {chunk_start + CHUNK}/{TIER_C_ROWS} rows written ({elapsed:.0f}s elapsed)")

    # Named ranges (just one — agent uses metric_lookup elsewhere)
    for name, (sheet_name, cell) in NAMED_RANGES.items():
        try:
            sheets.create_named_range(sid, name, f"{sheet_name}!{cell}")
        except Exception as e:
            _log(f"WARN: named_range {name} on Tier C: {e}")

    fixtures["tier_c"] = sid
    _save_fixtures(fixtures)
    total = time.perf_counter() - started
    _log(f"TIER C complete: 1 book × {TIER_C_ROWS*TIER_C_COLS} cells in {total/60:.1f}min")
    return sid


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    _require_live()
    fixtures = _load_fixtures()
    root_id = _claude_test_root()
    production_id = _ensure_production_folder(fixtures, root_id)
    _log(f"production folder: {production_id}")

    overall_started = time.perf_counter()

    try:
        build_tier_a(fixtures, production_id)
        build_tier_b(fixtures, production_id)
        build_tier_c(fixtures, production_id)
    except KeyboardInterrupt:
        _log("interrupted; partial state saved to .data/phase14_fixtures.json")
        return 1
    except Exception as e:
        _log(f"FATAL: {type(e).__name__}: {e}")
        return 1

    total = time.perf_counter() - overall_started
    fixtures["build_completed_at"] = dt.datetime.utcnow().isoformat() + "Z"
    fixtures["build_duration_min"] = round(total / 60, 1)
    _save_fixtures(fixtures)

    _log("=" * 60)
    _log(f"ALL TIERS BUILT in {total/60:.1f}min")
    _log(f"  Tier A: {len(fixtures.get('tier_a', []))} books")
    _log(f"  Tier B: {len(fixtures.get('tier_b', []))} books")
    _log(f"  Tier C: {fixtures.get('tier_c', '(missing)')}")
    _log(f"IDs saved to {FIXTURES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
