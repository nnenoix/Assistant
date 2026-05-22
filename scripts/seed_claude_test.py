"""Seed the CLAUDE-TEST folder on egor.titt@gmail.com with deliberately
messy test data for integration tests.

Usage:
  # First time only — creates CLAUDE-TEST root folder, persists its ID:
  python scripts/seed_claude_test.py --bootstrap-only

  # Bootstrap + populate `CLAUDE-TEST/seed/` with all available seeds:
  python scripts/seed_claude_test.py

  # Re-seed everything (skips existing root, refills the seed subfolder
  # with new timestamped artifacts so history accumulates):
  python scripts/seed_claude_test.py --reseed

This script lives in `scripts/` (not `src/`) because it's a one-shot
operator tool, not part of the runtime. Each function is idempotent
within a run — re-running creates timestamped sub-artifacts rather
than mutating in place.

Per the plan: CLAUDE-TEST is NEVER auto-cleaned. The seeder grows the
folder over time so we can see history.

Currently seeds (Phase 0):
  - 3 Google Sheets with garbage-but-realistic financial data
  - 5 receipt images (PNG, with Cyrillic text and digits)
  - 1 Calendar event tagged [CLAUDE-TEST]

Will be extended (later phases):
  - Phase 7: contracts/notes Google Docs with placeholders
  - Phase 8: Slides templates
  - Phase 9: Google Forms, Google Tasks list, Google Contacts entries
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path

# Make `src.*` importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.tools import drive, sheets, calendar

CONFIG_PATH = DATA_DIR / "integration_test_config.json"
ACCOUNT = "main"


# -------- bootstrap --------

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_folder_by_name(parent_id: str, name: str) -> str | None:
    listing = drive.list_files(folder_id=parent_id, account=ACCOUNT, page_size=200)
    for f in listing.get("files", []):
        if f.get("name") == name and f.get("mimeType") == "application/vnd.google-apps.folder":
            return f["id"]
    return None


def bootstrap() -> dict:
    """Idempotent: find-or-create CLAUDE-TEST root in `My Drive`, persist its ID."""
    cfg = _load_config()
    if cfg.get("claude_test_folder_id"):
        # Sanity-check it still exists by reading metadata
        try:
            meta = drive.get_metadata(cfg["claude_test_folder_id"], account=ACCOUNT)
            print(f"  CLAUDE-TEST exists ({meta['id']}): {meta.get('webViewLink', 'no link')}")
            return cfg
        except Exception as e:
            print(f"  stored folder id {cfg['claude_test_folder_id']} unreachable: {e}; recreating...")
    # Look up by name first to avoid duplicates
    existing = _find_folder_by_name("root", "CLAUDE-TEST")
    if existing:
        print(f"  found existing CLAUDE-TEST folder by name: {existing}")
        fid = existing
    else:
        created = drive.create_folder("root", "CLAUDE-TEST", account=ACCOUNT)
        fid = created["id"]
        print(f"  created CLAUDE-TEST folder: {fid}")
    cfg = {
        "account": ACCOUNT,
        "claude_test_folder_id": fid,
        "created_at": dt.datetime.utcnow().isoformat() + "Z",
    }
    _save_config(cfg)
    print(f"  saved config to {CONFIG_PATH}")
    return cfg


# -------- seed: sheets --------

def _seed_subfolder(root_id: str) -> str:
    """Return CLAUDE-TEST/seed/ id (create if needed)."""
    existing = _find_folder_by_name(root_id, "seed")
    if existing:
        return existing
    return drive.create_folder(root_id, "seed", account=ACCOUNT)["id"]


def _ts_suffix() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")


def seed_sheets_opiu_style(seed_id: str) -> str:
    """Create an «ОПиУ-стиль» book — 4 sheets, mixed languages, intentional gaps.

    Mirrors what we saw in the Panin failure: «Год факт» / «Год план» / «Месяцы»
    tabs with cumulative columns, irregular headers, blank trailing rows.
    """
    title = f"OPiU-seed-{_ts_suffix()}"
    ss = sheets.create_spreadsheet(title, account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, seed_id, account=ACCOUNT)

    # Rename default Sheet1 to «Год факт», then add three more sheets
    meta = sheets.get_metadata(sid, account=ACCOUNT)
    default_sheet_id = meta["sheets"][0]["properties"]["sheetId"]
    svc = sheets._service(ACCOUNT)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": default_sheet_id, "title": "Год факт"},
                "fields": "title",
            }},
            {"addSheet": {"properties": {"title": "Год план"}}},
            {"addSheet": {"properties": {"title": "Месяцы"}}},
            {"addSheet": {"properties": {"title": "Год факт с дырками"}}},
        ]},
    ).execute()

    # Year-actual: clean enough
    sheets.write_range(sid, "'Год факт'!A1:M3", [
        ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"],
        ["Выручка", 1_200_000, 1_350_000, 1_500_000, 1_700_000, 1_650_000, 1_900_000, 2_100_000, 2_050_000, 1_980_000, 2_200_000, 2_400_000, 2_500_000],
        ["Чистая прибыль", 120_000, 135_000, 150_000, 170_000, 165_000, 190_000, 210_000, 205_000, 198_000, 220_000, 240_000, 250_000],
    ], account=ACCOUNT)

    # Year-plan: missing values, mixed types, intentional inconsistency
    sheets.write_range(sid, "'Год план'!A1:M3", [
        ["", "янв", "фев", "Q1", "апр", "май", "июн", "Q2", "июл", "авг", "сен", "Q3", "год"],
        ["Выручка план", 1_000_000, 1_200_000, "=B2+C2", "", "", "", "", 2_000_000, 2_100_000, 2_200_000, "", 25_000_000],
        ["Прибыль план", "10%", "12%", "", "12%", "15%", "", "", "", "", "", "", ""],  # mixes % and absolute
    ], account=ACCOUNT)

    # Месяцы: a column-oriented form (one row per month) — different layout
    monthly = [["Месяц", "Выручка", "Чист. прибыль", "Маржа, %"]]
    monthly += [[m, random.randint(800_000, 3_000_000), random.randint(50_000, 400_000), round(random.uniform(0.04, 0.18), 3)]
                for m in ["январь", "февраль", "март", "апрель", "май", "июнь",
                         "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]]
    sheets.write_range(sid, "'Месяцы'!A1", monthly, account=ACCOUNT)

    # Year-actual-with-holes: 5 blank rows in middle, mixed currencies, typos
    sheets.write_range(sid, "'Год факт с дырками'!A1:E10", [
        ["", "USD", "EUR", "RUB", "Note"],
        ["Q1", 12_500, 11_300, 1_100_000, "ок"],
        ["", "", "", "", ""],
        ["", "", "", "", ""],
        ["Q2", 14_200, "", 1_290_000, "EUR not reported"],
        ["", "", "", "", ""],
        ["Q3", 13_800, 12_400, 1_240_000, "оплата задерживается"],
        ["Q4", "тбд", "тбд", "тбд", "тбд"],
        ["", "", "", "", ""],
        ["Итого", "=SUM(B2:B8)", "=SUM(C2:C8)", "=SUM(D2:D8)", ""],
    ], account=ACCOUNT)

    print(f"  + sheet '{title}' ({sid})")
    return sid


def _default_sheet_name(sid: str) -> str:
    """Return the (possibly localized) name of the first/default tab."""
    meta = sheets.get_metadata(sid, account=ACCOUNT)
    return meta["sheets"][0]["properties"]["title"]


def seed_sheets_abc_style(seed_id: str) -> str:
    """Create an «ABC-стиль» book — 1000 SKUs with revenue/qty/profit, dirty."""
    title = f"ABC-seed-{_ts_suffix()}"
    ss = sheets.create_spreadsheet(title, account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, seed_id, account=ACCOUNT)
    default = _default_sheet_name(sid)

    rows = [["SKU", "Название", "Выручка ₽", "Кол-во", "Себестоимость ₽", "Прибыль ₽"]]
    rng = random.Random(2026)
    brands = ["IdealNight", "SensesAura", "VelvetSkin", "Альтер Хим", "Test_brand", ""]
    for i in range(1, 1001):
        brand = rng.choice(brands)
        revenue = rng.randint(500, 200_000)
        qty = rng.randint(1, 800)
        cost = int(revenue * rng.uniform(0.3, 0.95))
        profit = revenue - cost
        # Inject mess every 30 rows
        if i % 30 == 0:
            revenue = ""  # blank
        if i % 50 == 0:
            brand = brand + " "  # trailing space
        sku = f"SKU-{i:04d}" if i % 100 != 0 else f"SKU-{i:04d}-DUP"  # rare dup-looking
        rows.append([sku, brand, revenue, qty, cost, profit])
    sheets.write_range(sid, f"'{default}'!A1", rows, account=ACCOUNT)
    print(f"  + sheet '{title}' ({sid}) — 1000 SKUs")
    return sid


def seed_sheets_long_log(seed_id: str) -> str:
    """Create a long event log — 10 000 rows. For paged-traversal tests."""
    title = f"Log-seed-{_ts_suffix()}"
    ss = sheets.create_spreadsheet(title, account=ACCOUNT)
    sid = ss["spreadsheetId"]
    drive.move(sid, seed_id, account=ACCOUNT)
    default = _default_sheet_name(sid)

    rng = random.Random(42)
    events = ["login", "click", "purchase", "logout", "error", "search"]
    header = ["ts", "user_id", "event", "value", "meta"]
    sheets.write_range(sid, f"'{default}'!A1:E1", [header], account=ACCOUNT)

    # Append in 1000-row chunks to avoid one massive request
    base_ts = dt.datetime(2026, 1, 1)
    for chunk_idx in range(10):
        chunk = []
        for i in range(1000):
            ts = base_ts + dt.timedelta(minutes=chunk_idx * 1000 + i * 7)
            chunk.append([
                ts.isoformat(),
                rng.randint(1, 5000),
                rng.choice(events),
                round(rng.uniform(0, 1500), 2),
                f"meta-{rng.randint(0, 9)}",
            ])
        sheets.append_rows(sid, f"'{default}'!A:E", chunk, account=ACCOUNT)
    print(f"  + sheet '{title}' ({sid}) — 10 000 rows")
    return sid


# -------- seed: receipts (local PNG generation, then Drive upload) --------

def seed_receipts(seed_id: str) -> list[str]:
    """Generate 5 simple PNG 'receipts' with Cyrillic text + digits, upload."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        print(f"  ! PIL not available, skipping receipts: {e}")
        return []

    receipts_local = DATA_DIR / "seed_receipts_local"
    receipts_local.mkdir(parents=True, exist_ok=True)

    # Make sure there's a receipts subfolder under seed
    receipts_id = _find_folder_by_name(seed_id, "Receipts")
    if not receipts_id:
        receipts_id = drive.create_folder(seed_id, "Receipts", account=ACCOUNT)["id"]

    items = [
        ("coffee.png", "ООО Кофейня\n2026-05-20\nЛатте 250 мл  390 ₽\nКруассан    220 ₽\nИТОГО       610 ₽"),
        ("taxi.png", "Яндекс.Такси\n2026-05-19 14:32\nСтоимость   742 ₽\nЧаевые      50 ₽\nИтого     792 ₽"),
        ("market.png", "Пятёрочка №432\nЯйцо С0 (10)  149.90 ₽\nХлеб Бородинский  72.50 ₽\nСыр Российский 350g  389.00 ₽\nИТОГО:   611.40 ₽"),
        ("garage.png", "AVTO-Service\nЗамена масла  3 500 ₽\nФильтр воздушный  900 ₽\nИТОГО  4 400 ₽\nГарантия 30 дней"),
        ("blurry.png", "P  yatё ro chka\nКака я-то\nцена 999\nИТО ГО ???"),  # intentionally fragmented for OCR robustness
    ]
    uploaded: list[str] = []
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    for name, text in items:
        local = receipts_local / name
        img = Image.new("RGB", (400, 220), "white")
        draw = ImageDraw.Draw(img)
        draw.multiline_text((15, 15), text, fill="black", font=font, spacing=4)
        img.save(local, "PNG")
        rec = drive.upload(str(local), receipts_id, name=name, mime_type="image/png", account=ACCOUNT)
        uploaded.append(rec["id"])
        print(f"    + receipt {name} ({rec['id']})")
    print(f"  + receipts folder ({receipts_id}) — {len(uploaded)} files")
    return uploaded


# -------- seed: calendar --------

def seed_calendar_event() -> str | None:
    """Create one calendar event tagged [CLAUDE-TEST]."""
    try:
        tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        start = tomorrow.strftime("%Y-%m-%d %H:%M")
        ev = calendar.create_event(
            summary="[CLAUDE-TEST] seed event",
            start=start,
            description="Created by scripts/seed_claude_test.py. Safe to delete.",
            account=ACCOUNT,
        )
        print(f"  + calendar event {ev.get('id', '?')} at {start}")
        return ev.get("id")
    except Exception as e:
        print(f"  ! calendar event skipped: {type(e).__name__}: {e}")
        return None


# -------- main --------

def main() -> None:
    ap = argparse.ArgumentParser(description="Seed CLAUDE-TEST on egor.titt@gmail.com")
    ap.add_argument("--bootstrap-only", action="store_true",
                    help="Only create the CLAUDE-TEST root folder + config file; no seed artifacts.")
    ap.add_argument("--reseed", action="store_true",
                    help="Force a fresh seed run even if seed/ folder already exists.")
    ap.add_argument("--skip-sheets", action="store_true", help="Skip Sheets seeding.")
    ap.add_argument("--skip-receipts", action="store_true", help="Skip receipts seeding.")
    ap.add_argument("--skip-calendar", action="store_true", help="Skip calendar event seeding.")
    args = ap.parse_args()

    print("== bootstrap CLAUDE-TEST root ==")
    cfg = bootstrap()
    root_id = cfg["claude_test_folder_id"]

    if args.bootstrap_only:
        print("\nDone (bootstrap only). CLAUDE-TEST root ready.")
        return

    print("\n== seed CLAUDE-TEST/seed/ ==")
    seed_id = _seed_subfolder(root_id)
    print(f"  seed folder: {seed_id}")

    if not args.skip_sheets:
        print("\n-- sheets --")
        seed_sheets_opiu_style(seed_id)
        seed_sheets_abc_style(seed_id)
        seed_sheets_long_log(seed_id)
    if not args.skip_receipts:
        print("\n-- receipts --")
        seed_receipts(seed_id)
    if not args.skip_calendar:
        print("\n-- calendar --")
        seed_calendar_event()

    print("\nDone.")


if __name__ == "__main__":
    main()
