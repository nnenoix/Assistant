"""Stress test — push every major tool with realistic large-scale data.

Target wall-clock: ~60 minutes. Live API only — requires LIVE_GOOGLE_TESTS=1.

Stages (sequential, with progress streamed to log):

  1. Giant spreadsheet — create + populate 250 000 rows × 8 cols
  2. Query aggregations — 15 server-side SELECTs against the giant sheet
  3. Full iter_rows traverse — read every row in chunks of 5000
  4. Profile + summarize — server-side column stats
  5. Wide sheet — 200 columns × 5000 rows
  6. Drive flood — create 200 small spreadsheets in one folder; list/search/name_patterns
  7. Batch verify_claim — 50 sources × 3 runs
  8. 200-page PDF — reportlab with cyrillic
  9. Concurrent reads — 20 parallel sheets reads via asyncio
  10. Mega reply_check — 200KB draft with 500 numbers
  11. Sheets metric_lookup on tall data — high-level resolver
  12. Cross-account fan-out — drive search account="*"

Each stage:
  - Streams "STAGE N start" / "STAGE N done in Ts" to progress.log
  - Captures p50/p95/max latencies for inner operations
  - On failure: logs error, continues to next stage (best-effort)

Artifacts:
  .data/sweep_results/stress_<ts>/progress.log     — live stream
  .data/sweep_results/stress_<ts>/per_stage.json   — detailed
  .data/sweep_results/stress_<ts>/summary.json     — aggregate
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.tools import (
    drive, sheets, pdf_gen, verify, reply_check, registry,
)


LIVE = os.environ.get("LIVE_GOOGLE_TESTS") == "1"
ACCOUNT = "main"

# Pacing between high-frequency calls to respect Google's 60 req/min/user
# quota (RetryingHttpRequest handles 429, but explicit pacing reduces noise).
PACE_SEC = 0.5

OUT_DIR: Path  # set in main()


def log(msg: str) -> None:
    """Log to file (UTF-8) and stdout (with ASCII fallback for non-UTF consoles).

    Windows default console encoding is cp1251 which can't render →, ×, ₽,
    Cyrillic etc. — encode-with-replace to keep the harness alive.
    """
    ts = dt.datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        # File always full UTF-8
        if OUT_DIR is not None:
            with (OUT_DIR / "progress.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    # Console: encode with replacement for unsupported chars
    try:
        encoding = sys.stdout.encoding or "ascii"
        safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)
    except Exception:
        # Last resort: ASCII-only
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)


# -------- helpers --------

def _create_test_folder() -> str:
    cfg_path = DATA_DIR / "integration_test_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    root_id = cfg["claude_test_folder_id"]
    listing = drive.list_files(folder_id=root_id, page_size=200, account=ACCOUNT)
    stress_parent = None
    for f in listing.get("files", []):
        if f.get("name") == "stress" and f.get("mimeType") == "application/vnd.google-apps.folder":
            stress_parent = f["id"]
            break
    if not stress_parent:
        stress_parent = drive.create_folder(root_id, "stress", account=ACCOUNT)["id"]
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    run = drive.create_folder(stress_parent, ts, account=ACCOUNT)
    return run["id"]


def _summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50_ms": round(statistics.median(values), 1),
        "p95_ms": round(sorted(values)[int(0.95 * (len(values) - 1))], 1) if len(values) > 1 else round(values[0], 1),
        "max_ms": round(max(values), 1),
        "total_s": round(sum(values) / 1000, 1),
    }


# ============================================================
# Stages
# ============================================================

def stage_1_giant_sheet(folder_id: str, target_rows: int = 250_000) -> dict:
    """Create + populate a 250k-row × 8-col spreadsheet."""
    log(f"STAGE 1: giant sheet — target {target_rows:,} rows")
    started = time.perf_counter()
    ss = sheets.create_spreadsheet(f"stress-giant-{target_rows}", account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, folder_id, account=ACCOUNT)
    meta = sheets.get_metadata(sid, account=ACCOUNT)
    default = meta["sheets"][0]["properties"]["title"]

    # Header
    sheets.write_range(sid, f"'{default}'!A1:H1", [
        ["ts", "user_id", "event", "value", "category", "country", "device", "meta"],
    ], account=ACCOUNT)

    chunk_size = 10_000
    chunks = target_rows // chunk_size
    rng = random.Random(2026)
    events = ["login", "click", "purchase", "logout", "search", "view", "error"]
    categories = ["A", "B", "C", "D", "E"]
    countries = ["RU", "KZ", "BY", "UA", "AM", "GE"]
    devices = ["web", "ios", "android", "desktop"]

    base_ts = dt.datetime(2026, 1, 1)
    append_latencies: list[float] = []

    for i in range(chunks):
        chunk = []
        for j in range(chunk_size):
            idx = i * chunk_size + j
            ts = base_ts + dt.timedelta(minutes=idx * 3)
            chunk.append([
                ts.isoformat(),
                rng.randint(1, 50_000),
                rng.choice(events),
                round(rng.uniform(0, 10_000), 2),
                rng.choice(categories),
                rng.choice(countries),
                rng.choice(devices),
                f"meta-{rng.randint(0, 99)}",
            ])
        t0 = time.perf_counter()
        sheets.append_rows(sid, f"'{default}'!A:H", chunk, account=ACCOUNT)
        elapsed = (time.perf_counter() - t0) * 1000
        append_latencies.append(elapsed)
        log(f"  chunk {i+1}/{chunks}: +{chunk_size} rows in {elapsed:.0f}ms")
        time.sleep(PACE_SEC)

    total = time.perf_counter() - started
    log(f"STAGE 1 done in {total:.1f}s — spreadsheet_id={sid}")
    return {
        "spreadsheet_id": sid,
        "default_sheet": default,
        "rows_written": target_rows,
        "total_s": round(total, 1),
        "append_latency": _summarize(append_latencies),
    }


def stage_2_queries(sid: str, default: str) -> dict:
    """Run 15 server-side QUERY aggregations against the giant sheet."""
    log("STAGE 2: 15 query aggregations")
    started = time.perf_counter()
    queries = [
        "SELECT count(A)",
        "SELECT C, count(C) GROUP BY C ORDER BY count(C) DESC",
        "SELECT F, count(F) GROUP BY F",
        "SELECT G, count(G) GROUP BY G",
        "SELECT sum(D)",
        "SELECT avg(D)",
        "SELECT min(D), max(D)",
        "SELECT C, sum(D) GROUP BY C ORDER BY sum(D) DESC",
        "SELECT F, sum(D) GROUP BY F",
        "SELECT G, avg(D) GROUP BY G",
        "SELECT C, F, count(A) GROUP BY C, F",
        "SELECT B, sum(D) GROUP BY B ORDER BY sum(D) DESC LIMIT 20",
        "SELECT C WHERE D > 5000 GROUP BY C",
        "SELECT count(A) WHERE C = 'A'",
        "SELECT sum(D) WHERE G = 'ios'",
    ]
    results = []
    latencies = []
    for q in queries:
        t0 = time.perf_counter()
        try:
            r = sheets.query(sid, f"'{default}'!A:H", q, account=ACCOUNT)
            elapsed = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed)
            row_count = r.get("row_count", 0)
            truncated = r.get("_meta", {}).get("truncated", False)
            results.append({"q": q[:60], "rows": row_count, "trunc": truncated, "ms": round(elapsed, 0)})
            log(f"  '{q[:50]}' → {row_count} rows in {elapsed:.0f}ms{' [TRUNC]' if truncated else ''}")
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            results.append({"q": q[:60], "error": str(e)[:200], "ms": round(elapsed, 0)})
            log(f"  '{q[:50]}' FAILED: {e}")
        time.sleep(PACE_SEC)

    total = time.perf_counter() - started
    log(f"STAGE 2 done in {total:.1f}s")
    return {
        "total_s": round(total, 1),
        "queries": results,
        "latency": _summarize(latencies),
    }


def stage_3_iter_rows(sid: str, default: str, total_rows: int) -> dict:
    """Traverse the entire spreadsheet via iter_rows."""
    log(f"STAGE 3: iter_rows full traverse of {total_rows:,} rows")
    started = time.perf_counter()
    offset = 0
    chunk_size = 5000
    rounds = 0
    latencies = []
    rows_seen = 0
    while True:
        t0 = time.perf_counter()
        r = sheets.iter_rows(sid, default, offset=offset, chunk_size=chunk_size, account=ACCOUNT)
        elapsed = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed)
        got = r.get("row_count", 0)
        rows_seen += got
        rounds += 1
        log(f"  round {rounds}: offset={offset} got={got} rows in {elapsed:.0f}ms")
        if not r.get("has_more"):
            break
        offset = r["next_offset"]
        time.sleep(PACE_SEC)
        # Safety guard
        if rounds > 100:
            log("  safety break at 100 rounds")
            break
    total = time.perf_counter() - started
    log(f"STAGE 3 done in {total:.1f}s — saw {rows_seen:,} rows in {rounds} rounds")
    return {
        "total_s": round(total, 1),
        "rows_seen": rows_seen,
        "rounds": rounds,
        "latency": _summarize(latencies),
    }


def stage_4_profile_summarize(sid: str, default: str) -> dict:
    """Server-side column stats + summarize."""
    log("STAGE 4: profile + summarize on giant sheet")
    started = time.perf_counter()
    t0 = time.perf_counter()
    prof = sheets.profile(sid, default, account=ACCOUNT)
    prof_ms = (time.perf_counter() - t0) * 1000
    log(f"  profile: {len(prof.get('columns', []))} columns in {prof_ms:.0f}ms")
    time.sleep(PACE_SEC)
    t0 = time.perf_counter()
    summ = sheets.summarize(sid, sample_rows=10, account=ACCOUNT)
    summ_ms = (time.perf_counter() - t0) * 1000
    sheet_info = summ.get("sheets", [{}])[0]
    data_rows_est = (sheet_info.get("_meta") or {}).get("data_rows_estimate")
    log(f"  summarize: data_rows_estimate={data_rows_est} in {summ_ms:.0f}ms")
    total = time.perf_counter() - started
    return {
        "total_s": round(total, 1),
        "profile_ms": round(prof_ms, 1),
        "summarize_ms": round(summ_ms, 1),
        "data_rows_estimate": data_rows_est,
        "profile_columns": len(prof.get("columns", [])),
    }


def stage_5_wide_sheet(folder_id: str) -> dict:
    """200 cols × 5000 rows — wide-and-tall."""
    log("STAGE 5: wide sheet 200 cols × 5000 rows")
    started = time.perf_counter()
    ss = sheets.create_spreadsheet("stress-wide", account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, folder_id, account=ACCOUNT)
    default = sheets.get_metadata(sid, account=ACCOUNT)["sheets"][0]["properties"]["title"]
    headers = [f"col_{i:03d}" for i in range(200)]
    sheets.write_range(sid, f"'{default}'!A1", [headers], account=ACCOUNT)

    rng = random.Random(7)
    chunk_size = 500
    chunks = 10  # 10 × 500 = 5000 rows
    append_latencies = []
    for i in range(chunks):
        rows = [[rng.randint(0, 1000) for _ in range(200)] for _ in range(chunk_size)]
        t0 = time.perf_counter()
        sheets.append_rows(sid, f"'{default}'!A:GR", rows, account=ACCOUNT)  # GR = col 200
        elapsed = (time.perf_counter() - t0) * 1000
        append_latencies.append(elapsed)
        log(f"  wide chunk {i+1}/{chunks}: {chunk_size} rows×200 cols in {elapsed:.0f}ms")
        time.sleep(PACE_SEC)

    # Test reads against the wide format
    t0 = time.perf_counter()
    summ = sheets.summarize(sid, sample_rows=3, account=ACCOUNT)
    summarize_ms = (time.perf_counter() - t0) * 1000
    cols_in_sample = (summ.get("sheets", [{}])[0].get("_meta") or {}).get("cols_in_sample")
    log(f"  summarize wide: cols_in_sample={cols_in_sample} in {summarize_ms:.0f}ms")

    total = time.perf_counter() - started
    log(f"STAGE 5 done in {total:.1f}s — sid={sid}")
    return {
        "spreadsheet_id": sid,
        "total_s": round(total, 1),
        "rows_written": chunks * chunk_size,
        "cols_in_sample": cols_in_sample,
        "append_latency": _summarize(append_latencies),
    }


def stage_6_drive_flood(folder_id: str, n: int = 200) -> dict:
    """Create N small spreadsheets in a single Drive folder, then list/search."""
    log(f"STAGE 6: drive flood — {n} files into one folder")
    started = time.perf_counter()
    flood = drive.create_folder(folder_id, "flood", account=ACCOUNT)
    flood_id = flood["id"]
    create_latencies = []
    rng = random.Random(99)
    brands = ["IdealNight", "SensesAura", "VelvetSkin", "AlterKhim", "TestBrand", "QuickFox"]
    months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    for i in range(n):
        name = f"{rng.choice(brands)} ОПиУ {rng.choice(months)} 2026 v{rng.randint(1,5)}"
        t0 = time.perf_counter()
        ss = sheets.create_spreadsheet(name, account=ACCOUNT)
        drive.move(ss["spreadsheetId"], flood_id, account=ACCOUNT)
        elapsed = (time.perf_counter() - t0) * 1000
        create_latencies.append(elapsed)
        if (i + 1) % 25 == 0:
            log(f"  created {i+1}/{n} files (last in {elapsed:.0f}ms)")
        time.sleep(0.3)  # slightly faster for drive ops

    # List, search, name_patterns
    log(f"  exercising drive ops on flood folder")
    t0 = time.perf_counter()
    listed = drive.list_files(folder_id=flood_id, page_size=200, account=ACCOUNT)
    list_ms = (time.perf_counter() - t0) * 1000
    log(f"  list_files: {len(listed['files'])} files, truncated={listed['_meta']['truncated']}, in {list_ms:.0f}ms")

    t0 = time.perf_counter()
    searched = drive.search("ОПиУ", page_size=200, account=ACCOUNT)
    search_ms = (time.perf_counter() - t0) * 1000
    log(f"  search 'ОПиУ': {len(searched['files'])} hits in {search_ms:.0f}ms")

    t0 = time.perf_counter()
    patterns = drive.name_patterns("ОПиУ", account=ACCOUNT)
    pat_ms = (time.perf_counter() - t0) * 1000
    log(f"  name_patterns: {patterns.get('total_files', 0)} files analysed in {pat_ms:.0f}ms")

    total = time.perf_counter() - started
    log(f"STAGE 6 done in {total:.1f}s")
    return {
        "total_s": round(total, 1),
        "files_created": n,
        "flood_folder_id": flood_id,
        "create_latency": _summarize(create_latencies),
        "list_ms": round(list_ms, 1),
        "search_ms": round(search_ms, 1),
        "name_patterns_ms": round(pat_ms, 1),
        "search_hits": len(searched["files"]),
        "name_patterns_files": patterns.get("total_files", 0),
    }


def stage_7_batch_verify(giant_sid: str, default: str) -> dict:
    """Run verify_claim with batches of 50 source_refs."""
    log("STAGE 7: batch verify_claim — 50 refs × 5 runs")
    started = time.perf_counter()
    refs_per_batch = 50
    # Build refs pointing to cells in the giant spreadsheet
    refs = [
        f"sheets:{giant_sid}:{default}!A{1 + i}"  # bare existence check (no `=value`)
        for i in range(refs_per_batch)
    ]
    run_latencies = []
    verdicts = []
    for run_i in range(5):
        t0 = time.perf_counter()
        r = verify.verify_claim(f"batch run {run_i}", refs)
        elapsed = (time.perf_counter() - t0) * 1000
        run_latencies.append(elapsed)
        verdicts.append(r["verdict"])
        log(f"  run {run_i+1}: verdict={r['verdict']} in {elapsed:.0f}ms ({refs_per_batch} refs)")
        time.sleep(PACE_SEC)

    total = time.perf_counter() - started
    log(f"STAGE 7 done in {total:.1f}s")
    return {
        "total_s": round(total, 1),
        "refs_per_batch": refs_per_batch,
        "verdicts": verdicts,
        "latency": _summarize(run_latencies),
    }


def stage_8_big_pdf() -> dict:
    """Generate a 200-page PDF report."""
    log("STAGE 8: 200-page PDF generation")
    started = time.perf_counter()
    sections = []
    rng = random.Random(13)
    for i in range(200):
        paras = [
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * rng.randint(2, 5),
            "Российский финансовый отчёт раздел " + str(i) + ". " + ("цифры данные " * rng.randint(3, 10)),
        ]
        table = {"headers": ["A", "B", "C", "D"],
                 "rows": [[rng.randint(0, 99) for _ in range(4)] for _ in range(10)]}
        sections.append({
            "heading": f"Раздел {i+1} — Stress Section",
            "paragraphs": paras,
            "table": table,
            "page_break_after": True,
        })
    dest = OUT_DIR / "stress_200page.pdf"
    t0 = time.perf_counter()
    r = pdf_gen.create_pdf({"title": "Stress Report", "sections": sections},
                           str(dest), kind="report")
    elapsed = (time.perf_counter() - t0) * 1000
    total = time.perf_counter() - started
    log(f"STAGE 8 done in {total:.1f}s — {r['bytes_written']:,} bytes")
    return {
        "total_s": round(total, 1),
        "pdf_path": str(dest),
        "bytes_written": r["bytes_written"],
        "generate_ms": round(elapsed, 1),
    }


async def _read_cell(sid: str, default: str, row: int, col: str) -> dict:
    t0 = time.perf_counter()
    r = await asyncio.to_thread(
        sheets.read_range, sid, f"'{default}'!{col}{row}", account=ACCOUNT,
    )
    return {"row": row, "col": col, "ms": (time.perf_counter() - t0) * 1000,
            "rows": r["_meta"]["row_count"]}


def stage_9_concurrent_reads(sid: str, default: str) -> dict:
    """Issue 20 sheets reads in parallel via asyncio."""
    log("STAGE 9: 20 concurrent reads via asyncio.gather")
    started = time.perf_counter()
    async def go():
        tasks = []
        for i in range(20):
            row = (i * 1000) + 2
            col = "ABCDEFGH"[i % 8]
            tasks.append(_read_cell(sid, default, row, col))
        return await asyncio.gather(*tasks, return_exceptions=True)
    results = asyncio.run(go())
    latencies = [r["ms"] for r in results if isinstance(r, dict)]
    fails = [r for r in results if isinstance(r, Exception)]
    total = time.perf_counter() - started
    log(f"STAGE 9 done in {total:.1f}s — {len(latencies)} ok, {len(fails)} fail")
    return {
        "total_s": round(total, 1),
        "ok": len(latencies),
        "fail": len(fails),
        "concurrent_latency": _summarize(latencies),
    }


def stage_10_mega_reply_check() -> dict:
    """Lint a 200KB reply with 500 numbers, half attributed."""
    log("STAGE 10: mega reply_check — 200KB, 500 numbers")
    started = time.perf_counter()
    rng = random.Random(42)
    lines = []
    n_numbers = 500
    n_attributed = 0
    for i in range(n_numbers):
        v = rng.randint(1000, 9_999_999)
        if i % 2 == 0:
            lines.append(f"Метрика {i}: {v:,} ₽ (Год факт!B{i+10}).")
            n_attributed += 1
        else:
            lines.append(f"Метрика {i}: {v:,} ₽.")
    # Pad with prose
    padding = "Lorem ipsum dolor sit amet. " * 1000
    draft = padding + "\n\n" + "\n".join(lines)
    log(f"  draft length: {len(draft):,} chars")

    t0 = time.perf_counter()
    r = reply_check.self_check(draft)
    elapsed = (time.perf_counter() - t0) * 1000

    total = time.perf_counter() - started
    log(f"STAGE 10 done in {total:.1f}s — {len(r['warnings'])} warnings in {elapsed:.1f}ms")
    return {
        "total_s": round(total, 1),
        "draft_chars": len(draft),
        "numbers_in_draft": n_numbers,
        "numbers_attributed": n_attributed,
        "warnings": len(r["warnings"]),
        "lint_ms": round(elapsed, 2),
    }


def stage_11_metric_lookup(folder_id: str) -> dict:
    """Build a tall financial-style sheet with explicit metrics; exercise metric_lookup."""
    log("STAGE 11: sheets_metric_lookup on built financial structure")
    started = time.perf_counter()
    ss = sheets.create_spreadsheet("stress-metrics", account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, folder_id, account=ACCOUNT)
    default = sheets.get_metadata(sid, account=ACCOUNT)["sheets"][0]["properties"]["title"]
    # Build wide P&L
    months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
              "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек", "Год"]
    metrics = ["Выручка", "Себестоимость", "Валовая прибыль", "Расходы", "EBITDA",
               "Амортизация", "Чистая прибыль", "Маржа"]
    rng = random.Random(2026)
    data = [[""] + months]
    for m in metrics:
        row = [m] + [rng.randint(100_000, 9_999_999) for _ in months]
        data.append(row)
    sheets.write_range(sid, f"'{default}'!A1", data, account=ACCOUNT)
    log(f"  built wide P&L: {len(metrics)} metrics × {len(months)} periods")

    test_queries = [
        ("Чистая прибыль", "Год"),
        ("Выручка", "Янв"),
        ("EBITDA", "Q2"),  # not present — expect candidates
        ("Маржа", "Дек"),
        ("Себестоимость", None),  # no period → last column
        ("несуществующая метрика", None),  # not found
    ]
    latencies = []
    results = []
    for metric, period in test_queries:
        t0 = time.perf_counter()
        r = sheets.metric_lookup(sid, metric, period=period, account=ACCOUNT)
        elapsed = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed)
        strategy = r["_meta"]["strategy"]
        value = r["value"]
        results.append({"metric": metric, "period": period, "value": value,
                        "strategy": strategy, "ms": round(elapsed, 0)})
        log(f"  '{metric}' / {period}: value={value} strategy={strategy} in {elapsed:.0f}ms")
        time.sleep(PACE_SEC)

    total = time.perf_counter() - started
    log(f"STAGE 11 done in {total:.1f}s")
    return {
        "spreadsheet_id": sid,
        "total_s": round(total, 1),
        "queries": results,
        "lookup_latency": _summarize(latencies),
    }


def stage_12_cross_account(folder_id: str) -> dict:
    """drive search with account='*'. Confirms multi-account fan-out works
    even when only one account is configured (degenerate case)."""
    log("STAGE 12: cross-account drive search account='*'")
    started = time.perf_counter()
    t0 = time.perf_counter()
    r = drive.search("ОПиУ", account="*")
    elapsed = (time.perf_counter() - t0) * 1000
    total = time.perf_counter() - started
    log(f"STAGE 12 done in {total:.1f}s — {len(r['files'])} files across "
        f"{len(r['_meta']['accounts_searched'])} account(s) in {elapsed:.0f}ms")
    return {
        "total_s": round(total, 1),
        "files_found": len(r["files"]),
        "accounts_searched": r["_meta"]["accounts_searched"],
        "per_account_counts": r["_meta"]["per_account_counts"],
        "elapsed_ms": round(elapsed, 1),
    }


# ============================================================
# Orchestration
# ============================================================

def main():
    global OUT_DIR
    if not LIVE:
        print("ERROR: set LIVE_GOOGLE_TESTS=1")
        sys.exit(2)

    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    OUT_DIR = DATA_DIR / "sweep_results" / f"stress_{ts}"
    OUT_DIR.mkdir(parents=True)
    log(f"== STRESS TEST starting — {ts} ==")
    log(f"output: {OUT_DIR}")
    overall_start = time.perf_counter()

    folder_id = _create_test_folder()
    log(f"stress folder: {folder_id}")

    stages_log: dict = {}

    def run_stage(name: str, fn, *args, **kwargs) -> dict | None:
        try:
            r = fn(*args, **kwargs)
            stages_log[name] = {"ok": True, **r}
            (OUT_DIR / "per_stage.json").write_text(
                json.dumps(stages_log, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return r
        except Exception as e:
            import traceback
            log(f"!! {name} FAILED: {type(e).__name__}: {e}")
            log(traceback.format_exc()[:2000])
            stages_log[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
            (OUT_DIR / "per_stage.json").write_text(
                json.dumps(stages_log, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return None

    # Allow reusing an existing giant spreadsheet via env to save ~2.5min
    reuse_sid = os.environ.get("REUSE_GIANT_SID")
    if reuse_sid:
        try:
            meta = sheets.get_metadata(reuse_sid, account=ACCOUNT)
            default = meta["sheets"][0]["properties"]["title"]
            giant_sid = reuse_sid
            log(f"STAGE 1 SKIPPED — reusing {reuse_sid} (default sheet={default!r})")
            stages_log["stage_1_giant_sheet"] = {"ok": True, "reused": True,
                                                   "spreadsheet_id": reuse_sid}
        except Exception as e:
            log(f"  reuse failed ({e}); creating fresh")
            reuse_sid = None
    if not reuse_sid:
        s1 = run_stage("stage_1_giant_sheet", stage_1_giant_sheet, folder_id, 250_000)
        giant_sid = s1["spreadsheet_id"] if s1 else None
        default = s1["default_sheet"] if s1 else None

    if giant_sid:
        run_stage("stage_2_queries", stage_2_queries, giant_sid, default)
        run_stage("stage_3_iter_rows", stage_3_iter_rows, giant_sid, default, 250_000)
        run_stage("stage_4_profile_summarize", stage_4_profile_summarize, giant_sid, default)

    run_stage("stage_5_wide_sheet", stage_5_wide_sheet, folder_id)
    run_stage("stage_6_drive_flood", stage_6_drive_flood, folder_id, 200)

    if giant_sid:
        run_stage("stage_7_batch_verify", stage_7_batch_verify, giant_sid, default)

    run_stage("stage_8_big_pdf", stage_8_big_pdf)

    if giant_sid:
        run_stage("stage_9_concurrent_reads", stage_9_concurrent_reads, giant_sid, default)

    run_stage("stage_10_mega_reply_check", stage_10_mega_reply_check)
    run_stage("stage_11_metric_lookup", stage_11_metric_lookup, folder_id)
    run_stage("stage_12_cross_account", stage_12_cross_account, folder_id)

    overall = time.perf_counter() - overall_start
    log(f"== STRESS TEST done in {overall/60:.1f}m ({overall:.0f}s) ==")
    summary = {
        "timestamp_utc": ts,
        "overall_s": round(overall, 1),
        "overall_min": round(overall / 60, 2),
        "stages_run": len(stages_log),
        "stages_ok": sum(1 for s in stages_log.values() if s.get("ok")),
        "stages_failed": sum(1 for s in stages_log.values() if not s.get("ok")),
        "stages": {k: {"ok": v.get("ok"), "total_s": v.get("total_s")} for k, v in stages_log.items()},
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    log(f"summary: {summary['stages_ok']}/{summary['stages_run']} stages OK in {summary['overall_min']}min")


if __name__ == "__main__":
    main()
