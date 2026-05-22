"""Comprehensive tool sweep — exercise every tool with 7-8 scenarios,
measure latency, capture _meta flags, log to disk.

Layout per run:
  .data/sweep_results/<utc_timestamp>/
    summary.json    — machine-readable per-tool stats
    log.txt         — line-per-call human log
    per_tool.json   — per-tool detailed results (scenarios + outcomes)

Scenarios live in `_SCENARIO_GENERATORS` — dict from category → list of
callable generators. Each generator yields {name, fn, args, expect} tuples
that the runner executes. The same fixture spreadsheet (created once per
sweep) is reused across sheets tools for speed.

Live API access is gated by env LIVE_GOOGLE_TESTS=1 — without it, only
the offline-runnable categories (verify, reply_check, tool_router,
pdf_gen, fx_rate, web, error_taxonomy) run.

Usage:
  uv run python scripts/sweep_tools.py                 # offline subset
  LIVE_GOOGLE_TESTS=1 uv run python scripts/sweep_tools.py  # full
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.tools import registry

LIVE = os.environ.get("LIVE_GOOGLE_TESTS") == "1"
ACCOUNT = "main"

# ============================================================
# Scenario types
# ============================================================

class Scenario:
    __slots__ = ("name", "fn", "args", "expect_ok", "expect_kind", "note")
    def __init__(self, name: str, fn: Callable, args: dict,
                 expect_ok: bool = True, expect_kind: str | None = None,
                 note: str = ""):
        self.name = name
        self.fn = fn
        self.args = args
        self.expect_ok = expect_ok
        self.expect_kind = expect_kind  # for error tests: expected error_kind
        self.note = note


# ============================================================
# Shared fixtures
# ============================================================

class Fixtures:
    """Live API fixtures created once per sweep, reused across tools."""
    def __init__(self):
        self.sweep_folder_id: str | None = None
        self.test_spreadsheet_id: str | None = None
        self.test_default_sheet_id: int | None = None
        self.test_default_sheet_name: str | None = None
        self.test_doc_id: str | None = None
        self.test_pres_id: str | None = None
        self.test_calendar_event_id: str | None = None
        self.test_task_list_id: str | None = None

    def ensure_sweep_folder(self) -> str | None:
        if not LIVE:
            return None
        if self.sweep_folder_id:
            return self.sweep_folder_id
        from src.tools import drive
        # Find or create CLAUDE-TEST/sweep/<timestamp>/
        cfg_path = DATA_DIR / "integration_test_config.json"
        if not cfg_path.exists():
            return None
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        root_id = cfg["claude_test_folder_id"]
        # Find or create 'sweep' subfolder
        listing = drive.list_files(folder_id=root_id, page_size=200, account=ACCOUNT)
        sweep_parent = None
        for f in listing.get("files", []):
            if f.get("name") == "sweep" and f.get("mimeType") == "application/vnd.google-apps.folder":
                sweep_parent = f["id"]
                break
        if not sweep_parent:
            created = drive.create_folder(root_id, "sweep", account=ACCOUNT)
            sweep_parent = created["id"]
        ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
        run = drive.create_folder(sweep_parent, ts, account=ACCOUNT)
        self.sweep_folder_id = run["id"]
        return self.sweep_folder_id

    def ensure_spreadsheet(self) -> str | None:
        if not LIVE:
            return None
        if self.test_spreadsheet_id:
            return self.test_spreadsheet_id
        folder = self.ensure_sweep_folder()
        if not folder:
            return None
        from src.tools import drive, sheets
        ss = sheets.create_spreadsheet("sweep-fixture", account=ACCOUNT)
        sid = ss["spreadsheetId"]
        drive.move(sid, folder, account=ACCOUNT)
        meta = sheets.get_metadata(sid, account=ACCOUNT)
        props = meta["sheets"][0]["properties"]
        self.test_default_sheet_id = props["sheetId"]
        self.test_default_sheet_name = props["title"]
        # Seed with mixed-quality data
        sheets.write_range(sid, f"'{props['title']}'!A1:E6", [
            ["", "Янв", "Фев", "Мар", "Год"],
            ["Выручка", 100, 200, 300, 600],
            ["Себестоимость", 50, 100, 150, 300],
            ["Чистая прибыль", 50, 100, 150, 300],
            ["Маржа", "10%", "12%", "", ""],
            ["", "", "", "", ""],
        ], account=ACCOUNT)
        self.test_spreadsheet_id = sid
        return sid


FIX = Fixtures()


# ============================================================
# Runner
# ============================================================

def _run_scenario(s: Scenario) -> dict:
    """Execute one scenario, capture timing + outcome.

    `pass_status`:
      - "pass"      — happy-path test succeeded OR error-test produced
                       expected error_kind
      - "fail"      — happy-path failed OR error-test didn't error
      - "wrong_kind" — error-test errored but with the wrong error_kind
    """
    started = time.perf_counter()
    record = {
        "name": s.name,
        "tool": s.fn.__module__.split(".")[-1] + "." + s.fn.__name__,
        "args_keys": sorted(s.args.keys()),
        "expect_ok": s.expect_ok,
        "expect_kind": s.expect_kind,
        "note": s.note,
    }
    try:
        result = s.fn(**s.args)
        elapsed_ms = (time.perf_counter() - started) * 1000
        record["elapsed_ms"] = round(elapsed_ms, 2)
        record["ok"] = True
        meta = None
        if isinstance(result, dict):
            meta = result.get("_meta")
            record["result_keys"] = sorted(result.keys())[:10]
        record["meta"] = meta if isinstance(meta, dict) else None
        if s.expect_ok:
            record["pass_status"] = "pass"
        else:
            record["pass_status"] = "fail"  # expected an error, got success
            record["unexpected_success"] = True
    except Exception as e:
        elapsed_ms = (time.perf_counter() - started) * 1000
        from src.tools.registry import _classify_exception
        kind, status = _classify_exception(e)
        record["elapsed_ms"] = round(elapsed_ms, 2)
        record["ok"] = False
        record["error_kind"] = kind
        record["http_status"] = status
        record["error_type"] = type(e).__name__
        record["error_msg"] = str(e)[:300]
        if s.expect_ok:
            record["pass_status"] = "fail"  # expected success, got error
        elif s.expect_kind and kind != s.expect_kind:
            record["pass_status"] = "wrong_kind"
            record["kind_matched"] = False
        else:
            record["pass_status"] = "pass"  # error expected, got correct kind (or any kind if no expect_kind)
            record["kind_matched"] = (kind == s.expect_kind) if s.expect_kind else None
    return record


def _log_line(record: dict) -> str:
    ps = record.get("pass_status", "?")
    status = {"pass": "PASS", "fail": "FAIL", "wrong_kind": "WKND"}.get(ps, "????")
    et = record.get("error_type", "")
    kind = record.get("error_kind", "")
    err = f" [{et}/{kind}]" if not record["ok"] else ""
    meta = record.get("meta") or {}
    meta_summary = ""
    if meta:
        flags = []
        if meta.get("truncated"): flags.append("trunc")
        if meta.get("empty_reason"): flags.append(f"empty:{meta['empty_reason']}")
        if meta.get("error_kind"): flags.append(f"errkind:{meta['error_kind']}")
        if flags:
            meta_summary = " meta=" + ",".join(flags)
    return f"{status} {record['elapsed_ms']:7.1f}ms  {record['tool']:50}  {record['name']:40}{err}{meta_summary}"


# ============================================================
# Scenario generators
# ============================================================

def gen_verify_claim() -> Iterable[Scenario]:
    from src.tools import verify
    f = verify.verify_claim
    yield Scenario("compact_no_refs", f, {"claim": "x", "source_refs": []})
    yield Scenario("compact_drive_existence", f, {"claim": "y", "source_refs": ["drive:fake_id"]})
    yield Scenario("invalid_kind_in_string", f, {"claim": "z", "source_refs": ["magic:abc=1"]})
    yield Scenario("dict_form", f, {"claim": "w", "source_refs": [{"kind": "drive_file", "file_id": "fake"}]})
    yield Scenario("mixed_forms", f, {
        "claim": "mix",
        "source_refs": [
            "sheets:SID:A1=42",
            {"kind": "drive_file", "file_id": "X"},
        ],
    })
    yield Scenario("missing_separator", f, {"claim": "bad", "source_refs": ["nosep"]})
    yield Scenario("with_expected_number", f, {"claim": "n", "source_refs": ["named:SID:Profit=3087967"]})
    yield Scenario("gmail_existence", f, {"claim": "g", "source_refs": ["gmail:MSG123"]})


def gen_reply_check() -> Iterable[Scenario]:
    from src.tools import reply_check
    f = reply_check.self_check
    yield Scenario("clean_reply", f, {"draft_reply": "Готово, всё хорошо."})
    yield Scenario("year_not_flagged", f, {"draft_reply": "Отчёт за 2026 готов."})
    yield Scenario("unattributed_currency", f, {"draft_reply": "Прибыль 3 087 967 ₽."})
    yield Scenario("attributed_currency", f, {"draft_reply": "Прибыль 3 087 967 ₽ (Год факт!B45)."})
    yield Scenario("completeness_with_truncated", f, {
        "draft_reply": "Это полный список файлов.",
        "recent_meta_flags": [{"truncated": True}],
    })
    yield Scenario("completeness_no_truncation", f, {
        "draft_reply": "Это полный список файлов.",
        "recent_meta_flags": [{"truncated": False}],
    })
    yield Scenario("multi_warning_mix", f, {
        "draft_reply": "12500 строк, прибыль 999999. Все файлы найдены.",
        "recent_meta_flags": [{"truncated": True}],
    })
    yield Scenario("empty_draft", f, {"draft_reply": ""})


def gen_tool_router() -> Iterable[Scenario]:
    from src import tool_router
    f = tool_router.classify_intent
    yield Scenario("financial", f, {"user_message": "Сравни прибыли брендов за 2026"})
    yield Scenario("email", f, {"user_message": "Найди письма от Олега"})
    yield Scenario("calendar", f, {"user_message": "Когда у меня свободно завтра"})
    yield Scenario("apps_script", f, {"user_message": "Почини скрипт WB"})
    yield Scenario("bank", f, {"user_message": "Распарси выписку Сбера"})
    yield Scenario("ocr", f, {"user_message": "Прочитай чек на изображении"})
    yield Scenario("short_message", f, {"user_message": "привет"})
    yield Scenario("empty", f, {"user_message": ""})


def gen_pdf_gen() -> Iterable[Scenario]:
    from src.tools import pdf_gen
    f = pdf_gen.create_pdf
    out_dir = DATA_DIR / "sweep_results" / "pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)
    yield Scenario("text_basic", f, {
        "content": "Привет, мир.\n\nВторой абзац.",
        "dest_path": str(out_dir / "text_basic.pdf"),
        "kind": "text", "title": "Test",
    })
    yield Scenario("text_no_title", f, {
        "content": "Без заголовка.",
        "dest_path": str(out_dir / "text_no_title.pdf"),
        "kind": "text",
    })
    yield Scenario("table_simple", f, {
        "content": {"headers": ["Бренд", "₽"], "rows": [["IN", 100], ["SA", -50]]},
        "dest_path": str(out_dir / "table.pdf"),
        "kind": "table", "title": "Q1",
    })
    yield Scenario("report_full", f, {
        "content": {
            "title": "Q1 Report",
            "sections": [
                {"heading": "Резюме", "paragraphs": ["Цифры неплохие."]},
                {"heading": "Таблица", "table": {"headers": ["A","B"], "rows": [["x", 1]]}},
            ],
        },
        "dest_path": str(out_dir / "report.pdf"),
        "kind": "report",
    })
    yield Scenario("empty_text", f, {
        "content": "", "dest_path": str(out_dir / "empty.pdf"), "kind": "text",
    })
    yield Scenario("unicode_heavy", f, {
        "content": "日本語 中文 한국어 हिन्दी العربية",
        "dest_path": str(out_dir / "unicode.pdf"), "kind": "text",
    })
    yield Scenario("unknown_kind", f, {
        "content": "x", "dest_path": str(out_dir / "nope.pdf"), "kind": "hologram",
    }, expect_ok=False, expect_kind="bad_input")
    yield Scenario("table_wrong_shape", f, {
        "content": "not a dict", "dest_path": str(out_dir / "bad.pdf"), "kind": "table",
    }, expect_ok=False, expect_kind="bad_input")


def gen_fx_rate() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import external
    f = external.fx_rate
    yield Scenario("usd_today", f, {"currency_code": "USD"})
    yield Scenario("eur_today", f, {"currency_code": "EUR"})
    yield Scenario("cny_today", f, {"currency_code": "CNY"})
    yield Scenario("gbp_today", f, {"currency_code": "GBP"})
    yield Scenario("usd_historical", f, {"currency_code": "USD", "date_iso": "2026-01-15"})
    yield Scenario("eur_historical", f, {"currency_code": "EUR", "date_iso": "2025-12-31"})
    yield Scenario("unknown_currency", f, {"currency_code": "ZZZ"})  # returns None but not error
    yield Scenario("bad_date_format", f, {"currency_code": "USD", "date_iso": "15-01-2026"},
                   expect_ok=False, expect_kind="bad_input")


def gen_web_fetch() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import web
    yield Scenario("example_text", web.fetch, {"url": "https://example.com/"})
    yield Scenario("example_html", web.fetch, {"url": "https://example.com/", "mode": "html"})
    yield Scenario("json_endpoint", web.fetch, {
        "url": "https://httpbin.org/json", "mode": "json", "timeout": 8,
    })
    yield Scenario("json_invalid_target", web.fetch, {
        "url": "https://example.com/", "mode": "json", "timeout": 8,
    })  # gets HTML, json parse fails, returns content=None
    yield Scenario("404_target", web.fetch, {
        "url": "https://httpbin.org/status/404", "timeout": 8,
    })
    yield Scenario("redirect_chain", web.fetch, {
        "url": "https://httpbin.org/redirect/2", "timeout": 8,
    })
    yield Scenario("unknown_mode", web.fetch, {"url": "https://example.com/", "mode": "blob"},
                   expect_ok=False, expect_kind="bad_input")
    yield Scenario("invalid_url_scheme", web.fetch, {"url": "not://valid"},
                   expect_ok=False, expect_kind="bad_input")


def gen_sheets() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import sheets
    sid = FIX.ensure_spreadsheet()
    if not sid:
        return
    name = FIX.test_default_sheet_name
    # 1. read_range happy path
    yield Scenario("read_range_basic", sheets.read_range, {
        "spreadsheet_id": sid, "range": f"'{name}'!A1:E2",
    })
    # 2. read_range empty
    yield Scenario("read_range_empty_row", sheets.read_range, {
        "spreadsheet_id": sid, "range": f"'{name}'!Z99:Z100",
    })
    # 3. formatted vs raw
    yield Scenario("read_range_formatted", sheets.read_range, {
        "spreadsheet_id": sid, "range": f"'{name}'!B2", "formatted": True,
    })
    # 4. batch_read multi-range
    yield Scenario("batch_read_3_ranges", sheets.batch_read, {
        "spreadsheet_id": sid,
        "ranges": [f"'{name}'!A1:A4", f"'{name}'!B2:E2", f"'{name}'!Z99"],
    })
    # 5. summarize
    yield Scenario("summarize_default", sheets.summarize, {"spreadsheet_id": sid})
    # 6. find_in_spreadsheet with labels
    yield Scenario("find_with_labels", sheets.find_in_spreadsheet, {
        "spreadsheet_id": sid, "query": "Чистая прибыль", "with_labels": True,
    })
    # 7. metric_lookup (high-level)
    yield Scenario("metric_lookup_year", sheets.metric_lookup, {
        "spreadsheet_id": sid, "metric": "Чистая прибыль", "period": "Год",
    })
    # 8. invalid range — error
    yield Scenario("invalid_range", sheets.read_range, {
        "spreadsheet_id": sid, "range": "NonExistent!A1:A2",
    }, expect_ok=False, expect_kind="bad_input")


def gen_drive() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import drive
    folder = FIX.ensure_sweep_folder()
    if not folder:
        return
    yield Scenario("list_files_root", drive.list_files, {"folder_id": "root", "page_size": 5})
    yield Scenario("list_files_sweep", drive.list_files, {"folder_id": folder, "page_size": 200})
    yield Scenario("search_claudetest", drive.search, {
        "name_contains": "sweep-fixture", "page_size": 10,
    })
    yield Scenario("search_no_match", drive.search, {
        "name_contains": "qqq_definitely_no_such_file_xxx", "page_size": 5,
    })
    yield Scenario("search_with_mime", drive.search, {
        "name_contains": "sweep", "mime_type": "spreadsheet", "page_size": 10,
    })
    yield Scenario("list_shared", drive.list_shared_with_me, {"page_size": 5})
    yield Scenario("name_patterns", drive.name_patterns, {"query": "OPiU"})
    yield Scenario("get_metadata_bad_id", drive.get_metadata, {
        "file_id": "definitely_not_a_real_file_id_12345",
    }, expect_ok=False, expect_kind="not_found")


def gen_calendar() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import calendar
    yield Scenario("list_default_window", calendar.list_events, {})
    yield Scenario("list_calendars", calendar.list_calendars, {})
    yield Scenario("list_narrow_window", calendar.list_events, {
        "time_min": "2026-05-21", "time_max": "2026-05-28",
    })
    yield Scenario("list_past_window", calendar.list_events, {
        "time_min": "2025-01-01", "time_max": "2025-01-07",
    })
    yield Scenario("list_with_query", calendar.list_events, {
        "time_min": "2026-01-01", "time_max": "2026-12-31",
        "query": "[CLAUDE-TEST]",
    })
    yield Scenario("get_event_bad_id", calendar.get_event, {"event_id": "nonsense"},
                   expect_ok=False, expect_kind="not_found")
    yield Scenario("find_free_time_short", calendar.find_free_time, {
        "duration_minutes": 30,
        "start_date": "2026-05-22", "end_date": "2026-05-23",
    })
    yield Scenario("list_with_max_results", calendar.list_events, {
        "time_min": "2026-01-01", "time_max": "2026-12-31", "max_results": 5,
    })


def gen_gmail() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import gmail
    yield Scenario("list_labels", gmail.list_labels, {})
    yield Scenario("search_small", gmail.search, {"query": "newer_than:30d", "max_results": 3})
    yield Scenario("search_specific", gmail.search, {"query": "from:google", "max_results": 5})
    yield Scenario("search_no_match", gmail.search, {
        "query": "subject:\"definitely no such subject xyz\"", "max_results": 5,
    })
    yield Scenario("search_attachments", gmail.search, {
        "query": "has:attachment newer_than:30d", "max_results": 3,
    })
    yield Scenario("search_unread", gmail.search, {"query": "is:unread", "max_results": 3})
    # Google returns 400 for malformed IDs (not 404), so error_kind is bad_input
    yield Scenario("get_message_bad_id", gmail.get_message, {"message_id": "fake_msg_id"},
                   expect_ok=False, expect_kind="bad_input")
    yield Scenario("search_starred", gmail.search, {"query": "is:starred", "max_results": 3})


def gen_docs() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import docs, drive
    folder = FIX.ensure_sweep_folder()
    if not folder:
        return
    # 1-2. Create doc + read
    created = None
    try:
        created = docs.create("sweep-doc", parent_folder_id=folder, account=ACCOUNT)
        FIX.test_doc_id = created["document_id"]
    except Exception:
        pass
    yield Scenario("create_returns_id", docs.create, {
        "title": "sweep-doc-2", "parent_folder_id": folder,
    })
    if FIX.test_doc_id:
        yield Scenario("read_empty_doc", docs.read, {"document_id": FIX.test_doc_id})
        yield Scenario("append_text", docs.append_text, {
            "document_id": FIX.test_doc_id, "text": "Привет."
        })
        yield Scenario("append_with_style", docs.append_text, {
            "document_id": FIX.test_doc_id, "text": "Heading", "style": "h1",
        })
        yield Scenario("replace_placeholders", docs.replace_text, {
            "document_id": FIX.test_doc_id, "replacements": {"Heading": "Заголовок"},
        })
        yield Scenario("read_after_edits", docs.read, {"document_id": FIX.test_doc_id})
        yield Scenario("read_bad_id", docs.read, {"document_id": "definitely_fake_doc_id"},
                       expect_ok=False, expect_kind="not_found")
    yield Scenario("create_no_folder", docs.create, {"title": "orphan-doc"})


def gen_slides() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import slides
    folder = FIX.ensure_sweep_folder()
    if not folder:
        return
    pres_id = None
    try:
        result = slides.create("sweep-pres", parent_folder_id=folder, account=ACCOUNT)
        pres_id = result["presentation_id"]
        FIX.test_pres_id = pres_id
    except Exception:
        pass
    yield Scenario("create_pres", slides.create, {"title": "sweep-pres-2", "parent_folder_id": folder})
    if pres_id:
        yield Scenario("read_default", slides.read, {"presentation_id": pres_id})
        yield Scenario("add_slide_default", slides.add_slide, {"presentation_id": pres_id})
        yield Scenario("add_slide_layout", slides.add_slide, {
            "presentation_id": pres_id, "layout": "TITLE_AND_BODY",
        })
        yield Scenario("add_slide_bad_layout", slides.add_slide, {
            "presentation_id": pres_id, "layout": "NONSENSE",
        }, expect_ok=False, expect_kind="bad_input")
        yield Scenario("replace_empty_placeholders", slides.replace_placeholders, {
            "presentation_id": pres_id, "replacements": {"{title}": "Заголовок"},
        })
        yield Scenario("read_bad_id", slides.read, {"presentation_id": "fake_pres"},
                       expect_ok=False, expect_kind="not_found")
    yield Scenario("replace_empty_dict", slides.replace_placeholders, {
        "presentation_id": pres_id or "x", "replacements": {},
    })


def gen_tasks() -> Iterable[Scenario]:
    if not LIVE:
        return
    from src.tools import tasks as gtasks
    yield Scenario("list_lists", gtasks.list_lists, {})
    # Create a temp list, exercise it, clean up at end
    list_id = None
    try:
        result = gtasks.create_list("[sweep]")
        list_id = result["list_id"]
        FIX.test_task_list_id = list_id
    except Exception:
        pass
    if list_id:
        yield Scenario("create_simple_task", gtasks.create, {
            "list_id": list_id, "title": "test task 1",
        })
        yield Scenario("create_task_with_due", gtasks.create, {
            "list_id": list_id, "title": "due task", "due": "2026-12-31",
        })
        yield Scenario("create_task_with_notes", gtasks.create, {
            "list_id": list_id, "title": "notes task", "notes": "long description here",
        })
        yield Scenario("list_tasks", gtasks.list_tasks, {"list_id": list_id})
        yield Scenario("list_show_completed", gtasks.list_tasks, {
            "list_id": list_id, "show_completed": True,
        })
        # Google Tasks returns 400 for unknown list_id (not 404)
        yield Scenario("list_bad_id", gtasks.list_tasks, {"list_id": "fake_list_xxx"},
                       expect_ok=False, expect_kind="bad_input")
    yield Scenario("list_lists_again", gtasks.list_lists, {})


def gen_self_introspection() -> Iterable[Scenario]:
    from src.tools import self_heal
    yield Scenario("list_tools", self_heal.self_list_tools, {})
    yield Scenario("git_status", self_heal.self_git_status, {})
    yield Scenario("git_diff_unstaged", self_heal.self_git_diff, {})
    yield Scenario("git_diff_staged", self_heal.self_git_diff, {"staged": True})
    # Read a known source file
    yield Scenario("read_source_self_heal", self_heal.self_read_source, {
        "path": "src/tools/self_heal.py",
    })
    yield Scenario("read_source_nonexistent", self_heal.self_read_source, {
        "path": "src/nonexistent_xxx.py",
    }, expect_ok=False, expect_kind="not_found")
    yield Scenario("smoke_test", self_heal.self_smoke_test, {})
    yield Scenario("read_source_with_path_escape", self_heal.self_read_source, {
        "path": "../../etc/passwd",
    }, expect_ok=False, expect_kind="bad_input")


def gen_analytics() -> Iterable[Scenario]:
    from src.tools import analytics
    rows = [
        {"sku": "A", "revenue": 1000, "qty": 10, "profit": 200},
        {"sku": "B", "revenue": 500, "qty": 5, "profit": 100},
        {"sku": "C", "revenue": 100, "qty": 1, "profit": 10},
        {"sku": "D", "revenue": 50, "qty": 1, "profit": -5},
    ]
    yield Scenario("abc_basic", analytics.abc_analysis, {"rows": rows})
    yield Scenario("abc_with_costs", analytics.abc_analysis, {
        "rows": rows, "costs": [{"sku": "A", "cost": 80}, {"sku": "B", "cost": 80}],
    })
    yield Scenario("abc_empty_rows", analytics.abc_analysis, {"rows": []})
    yield Scenario("abc_one_sku", analytics.abc_analysis, {"rows": [rows[0]]})
    yield Scenario("abc_split_revenue", analytics.abc_split, {"rows": rows, "metric": "revenue"})
    yield Scenario("abc_split_qty", analytics.abc_split, {"rows": rows, "metric": "qty"})
    yield Scenario("abc_split_profit", analytics.abc_split, {"rows": rows, "metric": "profit"})
    # abc_split is permissive — missing metric → rows tagged abc="?", no error
    yield Scenario("abc_split_missing_metric", analytics.abc_split,
                   {"rows": rows, "metric": "nonexistent"})


# ============================================================
# Main
# ============================================================

ALL_GENERATORS = {
    "verify": gen_verify_claim,
    "reply_check": gen_reply_check,
    "tool_router": gen_tool_router,
    "pdf_gen": gen_pdf_gen,
    "fx_rate": gen_fx_rate,
    "web": gen_web_fetch,
    "sheets": gen_sheets,
    "drive": gen_drive,
    "calendar": gen_calendar,
    "gmail": gen_gmail,
    "docs": gen_docs,
    "slides": gen_slides,
    "tasks": gen_tasks,
    "self": gen_self_introspection,
    "analytics": gen_analytics,
}


def main():
    out_root = DATA_DIR / "sweep_results"
    out_root.mkdir(exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = out_root / ts
    out_dir.mkdir()

    log_path = out_dir / "log.txt"
    per_tool_path = out_dir / "per_tool.json"
    summary_path = out_dir / "summary.json"

    all_results: dict[str, list[dict]] = {}
    log_lines: list[str] = []

    log_lines.append(f"== sweep_tools — {ts} ==")
    log_lines.append(f"LIVE_GOOGLE_TESTS={'1' if LIVE else '0'}")
    log_lines.append(f"Total tools registered: {len(registry.TOOLS)}")
    log_lines.append("")

    for category, gen in ALL_GENERATORS.items():
        scenarios = list(gen())
        if not scenarios:
            log_lines.append(f"-- {category}: SKIPPED (no scenarios — likely live-only and LIVE_GOOGLE_TESTS not set)")
            continue
        log_lines.append(f"-- {category} ({len(scenarios)} scenarios) --")
        cat_results: list[dict] = []
        for s in scenarios:
            rec = _run_scenario(s)
            cat_results.append(rec)
            log_lines.append("  " + _log_line(rec))
        all_results[category] = cat_results
        log_lines.append("")
        print(f"  done: {category} ({len(cat_results)} scenarios)")

    # Cleanup live fixtures (best-effort)
    if LIVE and FIX.test_task_list_id:
        try:
            from src.tools import tasks as gtasks
            gtasks._service(ACCOUNT).tasklists().delete(tasklist=FIX.test_task_list_id).execute()
        except Exception:
            pass

    # Summary
    summary: dict = {
        "timestamp_utc": ts,
        "live": LIVE,
        "total_tools": len(registry.TOOLS),
        "categories": {},
    }
    total_scenarios = 0
    total_passed = 0
    total_failed = 0
    total_wrong_kind = 0
    all_latencies: list[float] = []
    for cat, recs in all_results.items():
        passed = sum(1 for r in recs if r.get("pass_status") == "pass")
        failed = sum(1 for r in recs if r.get("pass_status") == "fail")
        wrong_kind = sum(1 for r in recs if r.get("pass_status") == "wrong_kind")
        lat = [r["elapsed_ms"] for r in recs]
        all_latencies.extend(lat)
        summary["categories"][cat] = {
            "scenarios": len(recs),
            "passed": passed,
            "failed": failed,
            "wrong_kind": wrong_kind,
            "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
            "latency_p95_ms": round(_p95(lat), 1) if lat else None,
            "latency_max_ms": round(max(lat), 1) if lat else None,
        }
        total_scenarios += len(recs)
        total_passed += passed
        total_failed += failed
        total_wrong_kind += wrong_kind
    summary["total_scenarios"] = total_scenarios
    summary["total_passed"] = total_passed
    summary["total_failed"] = total_failed
    summary["total_wrong_kind"] = total_wrong_kind
    if all_latencies:
        summary["global_latency_p50_ms"] = round(statistics.median(all_latencies), 1)
        summary["global_latency_p95_ms"] = round(_p95(all_latencies), 1)
    log_lines.append("== SUMMARY ==")
    log_lines.append(f"scenarios={total_scenarios}  passed={total_passed}  failed={total_failed}  wrong_kind={total_wrong_kind}")
    if all_latencies:
        log_lines.append(f"latency p50={summary['global_latency_p50_ms']}ms p95={summary['global_latency_p95_ms']}ms")

    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    per_tool_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults written to {out_dir}")
    print(f"  log.txt       — {len(log_lines)} lines")
    print(f"  per_tool.json — full per-scenario records")
    print(f"  summary.json  — aggregate")
    return out_dir


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = int(0.95 * (len(s) - 1))
    return s[idx]


if __name__ == "__main__":
    main()
