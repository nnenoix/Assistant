import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials
from src.config import DATA_DIR


DEFAULT_ACCOUNT = "main"
BACKUPS_DIR = DATA_DIR / "sheets_backups"
BACKUPS_DIR.mkdir(exist_ok=True)


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    # requestBuilder gives every request transparent retry+backoff on
    # 429 (per-user-per-minute Sheets read quota) and 5xx / transient
    # network errors. See src.auth.RetryingHttpRequest.
    return build(
        "sheets", "v4",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


# Locales where Google Sheets uses ';' as function-argument separator.
# Anywhere else (en_*, ja_JP, ko_KR, zh_CN, zh_TW, ...) uses ','.
_SEMICOLON_LOCALE_PREFIXES = (
    "ru", "uk", "be", "kk", "uz",  # Cyrillic
    "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk", "sl", "hr",
    "ro", "hu", "fi", "sv", "no", "da", "et", "lv", "lt", "el", "bg",
    "tr", "ar", "he",
)


@lru_cache(maxsize=64)
def _arg_sep(spreadsheet_id: str, account: str) -> str:
    """Return ';' for spreadsheets in European/Cyrillic locales, ',' for the rest.
    Cached per (spreadsheet_id, account).
    """
    try:
        loc = (
            _service(account).spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="properties.locale")
            .execute()
            .get("properties", {})
            .get("locale", "")
            .lower()
        )
    except Exception:
        return ","
    prefix = loc.split("_")[0] if loc else ""
    return ";" if prefix in _SEMICOLON_LOCALE_PREFIXES else ","


def _snapshot(spreadsheet_id: str, range_a1: str, account: str, op: str) -> str | None:
    """Read the current values of `range_a1` and persist as a backup.
    Returns the snapshot id on success, None on any failure — snapshotting
    must never block the actual write.
    """
    try:
        before = _service(account).spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_a1
        ).execute()
        values = before.get("values", [])
        if not isinstance(values, list):
            return None
        snap_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3]
        sheet_dir = BACKUPS_DIR / spreadsheet_id
        sheet_dir.mkdir(parents=True, exist_ok=True)
        path = sheet_dir / f"{snap_id}.json"
        path.write_text(
            json.dumps({
                "snapshot_id": snap_id,
                "spreadsheet_id": spreadsheet_id,
                "account": account,
                "range": range_a1,
                "op": op,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "values": values,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return snap_id
    except Exception:
        return None


def read_range(spreadsheet_id: str, range: str, formatted: bool = False, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read a range. Returns {values, _meta}.

    The `_meta` envelope lets the agent cite provenance:
      - `range_read`: what Sheets actually echoed back as the range
      - `empty_reason`: None | "no_data" — distinguishes "valid range, zero
        rows" from a genuinely populated read; invalid ranges raise HttpError
        upstream and surface as is_error to the model.

    `formatted=True` → `valueRenderOption=FORMATTED_VALUE`: agent sees the
    string as displayed in the UI (e.g. "3 087 967 ₽"). Default False sets
    `UNFORMATTED_VALUE` so numbers come back as numbers (Google's API
    default is FORMATTED_VALUE — strings — which breaks arithmetic).

    Phase 14E: opt-in TTL+LRU cache via env SHEETS_READ_CACHE=1.
    On hit, `_meta.from_cache=True`.
    """
    from src.tools._read_cache import CACHE, make_key
    cache_key = make_key(account, spreadsheet_id, range, formatted)
    cached = CACHE.get(cache_key)
    if cached is not None:
        # Shallow-copy _meta so we don't mutate the cached entry
        result = {"values": cached["values"], "_meta": {**cached["_meta"], "from_cache": True}}
        return result

    kwargs = {
        "spreadsheetId": spreadsheet_id,
        "range": range,
        "valueRenderOption": "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE",
    }
    resp = _service(account).spreadsheets().values().get(**kwargs).execute()
    values = resp.get("values", [])
    result = {
        "values": values,
        "_meta": {
            "range_read": resp.get("range", range),
            "row_count": len(values),
            "value_mode": "formatted" if formatted else "raw",
            "empty_reason": None if values else "no_data",
        },
    }
    CACHE.set(cache_key, result)
    return result


def batch_read(
    spreadsheet_id: str,
    ranges: list[str],
    formatted: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read multiple ranges in ONE HTTP call via `values.batchGet`.

    For cross-sheet aggregation: e.g., 4 brand workbooks' single «Год факт!B45»
    cells in one request instead of four. Each entry in `per_range` carries
    its own `range_read` so the agent can cite provenance per value.
    """
    if not ranges:
        return {"per_range": [], "_meta": {"requested_count": 0, "returned_count": 0, "empty_reason": "no_ranges"}}
    kwargs = {
        "spreadsheetId": spreadsheet_id,
        "ranges": list(ranges),
        "valueRenderOption": "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE",
    }
    resp = _service(account).spreadsheets().values().batchGet(**kwargs).execute()
    per_range = []
    for entry in resp.get("valueRanges", []):
        vals = entry.get("values", [])
        per_range.append({
            "range": entry.get("range"),
            "values": vals,
            "row_count": len(vals),
            "empty": not vals,
        })
    return {
        "per_range": per_range,
        "_meta": {
            "requested_count": len(ranges),
            "returned_count": len(per_range),
            "value_mode": "formatted" if formatted else "raw",
            "empty_reason": None if any(not p["empty"] for p in per_range) else "no_data",
        },
    }


def list_named_ranges(spreadsheet_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List every named range defined in the spreadsheet.

    A named range like `Чистая_прибыль_Год` lets you read a specific cell
    by NAME instead of guessing its A1 address. Returns A1 form for each.
    """
    svc = _service(account)
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="namedRanges,sheets.properties(sheetId,title)",
    ).execute()
    # Build sheetId → title for A1 conversion
    sheet_title_by_id = {
        s["properties"]["sheetId"]: s["properties"]["title"]
        for s in meta.get("sheets", [])
    }
    out = []
    for nr in meta.get("namedRanges", []):
        gr = nr.get("range", {})
        sheet_id = gr.get("sheetId")
        sheet_name = sheet_title_by_id.get(sheet_id, f"sheet#{sheet_id}")
        a1 = _grid_range_to_a1(sheet_name, gr)
        out.append({
            "name": nr.get("name"),
            "named_range_id": nr.get("namedRangeId"),
            "sheet_id": sheet_id,
            "sheet": sheet_name,
            "range": a1,
        })
    return {
        "named_ranges": out,
        "_meta": {
            "count": len(out),
            "empty_reason": None if out else "no_named_ranges",
        },
    }


def read_named_range(
    spreadsheet_id: str,
    name: str,
    formatted: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read the values stored at a named range.

    Sheets `values.get` accepts the named range identifier directly as its
    `range` parameter. Returns {values, _meta:{range_read, name_resolved}}.

    Phase 14E: cached via the same `read_range` cache keyed by (account,
    spreadsheet_id, name, formatted).
    """
    from src.tools._read_cache import CACHE, make_key
    # Prefix name with "named:" in the key so a literal range "MyName" can't
    # collide with a same-named named range
    cache_key = make_key(account, spreadsheet_id, f"named:{name}", formatted)
    cached = CACHE.get(cache_key)
    if cached is not None:
        return {"values": cached["values"], "_meta": {**cached["_meta"], "from_cache": True}}

    kwargs = {
        "spreadsheetId": spreadsheet_id,
        "range": name,
        "valueRenderOption": "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE",
    }
    resp = _service(account).spreadsheets().values().get(**kwargs).execute()
    values = resp.get("values", [])
    result = {
        "values": values,
        "_meta": {
            "name": name,
            "range_read": resp.get("range"),
            "row_count": len(values),
            "value_mode": "formatted" if formatted else "raw",
            "empty_reason": None if values else "no_data",
        },
    }
    CACHE.set(cache_key, result)
    return result


def create_named_range(
    spreadsheet_id: str,
    name: str,
    range: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Define a named range. `range` is A1 like "Sheet1!B45" or "Sheet1!B45:B45".

    Resolves the sheet name → sheetId and the A1 → row/col indices, then
    submits an `addNamedRange` batchUpdate request.
    """
    sheet_part, cell_part = _split_a1(range)
    svc = _service(account)
    sheet_meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    sheet_id = None
    for s in sheet_meta.get("sheets", []):
        if s["properties"]["title"] == sheet_part:
            sheet_id = s["properties"]["sheetId"]
            break
    if sheet_id is None:
        raise ValueError(f"sheet {sheet_part!r} not found in spreadsheet")

    grid_range = {"sheetId": sheet_id, **_a1_to_grid_indices(cell_part)}
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addNamedRange": {"namedRange": {
            "name": name,
            "range": grid_range,
        }}}]},
    ).execute()
    nr = resp.get("replies", [{}])[0].get("addNamedRange", {}).get("namedRange", {})
    return {
        "ok": True,
        "name": nr.get("name", name),
        "named_range_id": nr.get("namedRangeId"),
        "range": range,
    }


def duplicate_sheet(
    spreadsheet_id: str,
    source_sheet: int | str,
    new_name: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Duplicate a sheet/tab inside the SAME spreadsheet.

    `source_sheet` accepts the numeric sheetId OR the tab title (resolved
    via metadata). For cross-spreadsheet copy use `copy_sheet_to`.
    """
    source_sheet_id = _resolve_sheet_id(spreadsheet_id, source_sheet, account)
    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"duplicateSheet": {
            "sourceSheetId": source_sheet_id,
            "newSheetName": new_name,
        }}]},
    ).execute()
    props = resp.get("replies", [{}])[0].get("duplicateSheet", {}).get("properties", {})
    return {
        "new_sheet_id": props.get("sheetId"),
        "title": props.get("title"),
        "index": props.get("index"),
    }


def copy_sheet_to(
    source_spreadsheet_id: str,
    source_sheet: int | str,
    dest_spreadsheet_id: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Copy a tab into a DIFFERENT spreadsheet via `sheets.sheets().copyTo`.

    `source_sheet` accepts numeric sheetId OR tab title. The destination
    receives a new sheet titled «Copy of …» by Google — rename via
    batchUpdate updateSheetProperties if needed.
    """
    source_sheet_id = _resolve_sheet_id(source_spreadsheet_id, source_sheet, account)
    resp = _service(account).spreadsheets().sheets().copyTo(
        spreadsheetId=source_spreadsheet_id,
        sheetId=source_sheet_id,
        body={"destinationSpreadsheetId": dest_spreadsheet_id},
    ).execute()
    return {
        "copied_sheet_id": resp.get("sheetId"),
        "title": resp.get("title"),
        "dest_spreadsheet_id": dest_spreadsheet_id,
    }


# -------- A1 ↔ grid helpers (used by named ranges) --------

def _split_a1(a1: str) -> tuple[str, str]:
    """Split 'Sheet1!B45:C50' or "'Год факт'!B45" into (sheet_name, cells)."""
    if "!" not in a1:
        raise ValueError(f"need 'Sheet!Cell' form, got {a1!r}")
    sheet, cell = a1.split("!", 1)
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, cell


_CELL_RE = __import__("re").compile(r"^([A-Z]+)(\d+)$")


def _a1_cell_to_indices(cell: str) -> tuple[int, int]:
    """'B45' → (row_index_0, col_index_0). Returns (44, 1)."""
    m = _CELL_RE.match(cell.upper())
    if not m:
        raise ValueError(f"not an A1 cell: {cell!r}")
    col_letters, row_str = m.groups()
    col = 0
    for c in col_letters:
        col = col * 26 + (ord(c) - ord("A") + 1)
    return int(row_str) - 1, col - 1


def _a1_to_grid_indices(cells: str) -> dict:
    """'B45:C50' → {startRowIndex, endRowIndex, startColumnIndex, endColumnIndex}.

    Sheets' GridRange uses half-open intervals (start inclusive, end exclusive).
    Single-cell form 'B45' is treated as 'B45:B45'.
    """
    if ":" not in cells:
        cells = f"{cells}:{cells}"
    left, right = cells.split(":", 1)
    r1, c1 = _a1_cell_to_indices(left)
    r2, c2 = _a1_cell_to_indices(right)
    return {
        "startRowIndex": r1,
        "endRowIndex": r2 + 1,
        "startColumnIndex": c1,
        "endColumnIndex": c2 + 1,
    }


def _grid_range_to_a1(sheet_name: str, gr: dict) -> str:
    """Reverse — for surfacing named-range A1 back to the agent."""
    start_r = gr.get("startRowIndex", 0)
    end_r = gr.get("endRowIndex")
    start_c = gr.get("startColumnIndex", 0)
    end_c = gr.get("endColumnIndex")
    if end_r is None or end_c is None:
        # whole-sheet named range
        return f"'{sheet_name}'"
    start = f"{_col_to_a1(start_c)}{start_r + 1}"
    end = f"{_col_to_a1(end_c - 1)}{end_r}"  # end_r is exclusive, so end row = end_r (1-based)
    return f"'{sheet_name}'!{start}:{end}"


def _resolve_sheet_id(spreadsheet_id: str, sheet: str | int, account: str) -> int:
    """Accept either an int sheetId or a sheet title; return numeric sheetId."""
    if isinstance(sheet, int):
        return sheet
    svc = _service(account)
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet:
            return s["properties"]["sheetId"]
    raise ValueError(f"sheet {sheet!r} not found in {spreadsheet_id}")


def _build_grid_range(sheet_id: int, range_a1_cells: str | None) -> dict:
    """Build a GridRange dict. If `range_a1_cells` is None, returns whole-sheet."""
    gr = {"sheetId": sheet_id}
    if range_a1_cells:
        gr.update(_a1_to_grid_indices(range_a1_cells))
    return gr


# -------- Phase 2: formatting + charts + pivots + validation --------

_NUMBER_FORMAT_PRESETS = {
    "currency_rub": {"type": "CURRENCY", "pattern": "#,##0.00 [$₽-419]"},
    "currency_rub_int": {"type": "CURRENCY", "pattern": "#,##0 [$₽-419]"},
    "currency_usd": {"type": "CURRENCY", "pattern": '"$"#,##0.00'},
    "currency_eur": {"type": "CURRENCY", "pattern": "#,##0.00 [$€-2]"},
    "percent": {"type": "PERCENT", "pattern": "0.00%"},
    "percent_int": {"type": "PERCENT", "pattern": "0%"},
    "date_iso": {"type": "DATE", "pattern": "yyyy-mm-dd"},
    "date_ru": {"type": "DATE", "pattern": "dd.mm.yyyy"},
    "datetime_ru": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy HH:MM"},
    "number_2dp": {"type": "NUMBER", "pattern": "#,##0.00"},
    "number_int": {"type": "NUMBER", "pattern": "#,##0"},
    "text": {"type": "TEXT", "pattern": "@"},
}


def set_format(
    spreadsheet_id: str,
    range: str,
    preset: str | None = None,
    number_format: dict | None = None,
    background_color: dict | None = None,
    text_format: dict | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Apply formatting to a range.

    Either pass `preset` (one of: currency_rub, currency_rub_int, currency_usd,
    currency_eur, percent, percent_int, date_iso, date_ru, datetime_ru,
    number_2dp, number_int, text) OR a raw `number_format` dict like
    {"type": "CURRENCY", "pattern": "#,##0 [$₽-419]"}.

    Optional `background_color` = {"red": 0.9, "green": 0.95, "blue": 1.0}.
    Optional `text_format` = {"bold": True, "italic": False, "fontSize": 11}.
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)

    nf = number_format
    if nf is None and preset:
        if preset not in _NUMBER_FORMAT_PRESETS:
            raise ValueError(f"unknown preset {preset!r}; available: {sorted(_NUMBER_FORMAT_PRESETS)}")
        nf = _NUMBER_FORMAT_PRESETS[preset]

    cell_format: dict = {}
    fields: list[str] = []
    if nf is not None:
        cell_format["numberFormat"] = nf
        fields.append("userEnteredFormat.numberFormat")
    if background_color is not None:
        cell_format["backgroundColor"] = background_color
        fields.append("userEnteredFormat.backgroundColor")
    if text_format is not None:
        cell_format["textFormat"] = text_format
        fields.append("userEnteredFormat.textFormat")
    if not fields:
        raise ValueError("nothing to apply: pass preset, number_format, background_color, or text_format")

    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": gr,
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join(fields),
            },
        }]},
    ).execute()
    return {"ok": True, "applied_fields": fields, "range": range, "preset": preset, "raw": resp}


def freeze(
    spreadsheet_id: str,
    sheet: str | int,
    rows: int = 0,
    cols: int = 0,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Freeze N rows / M cols of a sheet so they stay visible while scrolling.

    `sheet` can be a sheet title or numeric sheetId. Pass `rows=1` to pin the
    header row.
    """
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet, account)
    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": cols},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            },
        }]},
    ).execute()
    return {"ok": True, "sheet_id": sheet_id, "frozen_rows": rows, "frozen_cols": cols}


def merge_cells(
    spreadsheet_id: str,
    range: str,
    merge_type: str = "MERGE_ALL",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Merge a rectangular range into one cell.

    `merge_type`: MERGE_ALL (single big cell), MERGE_COLUMNS (each col stays
    separate, rows merge), MERGE_ROWS (each row stays, cols merge).
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)
    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"mergeCells": {"range": gr, "mergeType": merge_type}}]},
    ).execute()
    return {"ok": True, "range": range, "merge_type": merge_type}


def unmerge_cells(spreadsheet_id: str, range: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Undo a merge inside a range."""
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)
    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"unmergeCells": {"range": gr}}]},
    ).execute()
    return {"ok": True, "range": range}


def set_data_validation(
    spreadsheet_id: str,
    range: str,
    kind: str,
    values: list[str] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    strict: bool = True,
    show_dropdown: bool = True,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Attach a validation rule to a range.

    `kind`:
      - "dropdown" — values is the list of allowed strings (shown as dropdown).
      - "number_between" — min_value <= cell <= max_value.
      - "checkbox" — TRUE/FALSE checkbox UI.
      - "remove" — clears any existing validation on the range.

    `strict=True` rejects invalid input; False just warns.
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)

    if kind == "remove":
        request = {"setDataValidation": {"range": gr}}
    elif kind == "dropdown":
        if not values:
            raise ValueError("dropdown needs `values` list")
        request = {"setDataValidation": {
            "range": gr,
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "strict": strict,
                "showCustomUi": show_dropdown,
            },
        }}
    elif kind == "number_between":
        if min_value is None or max_value is None:
            raise ValueError("number_between needs min_value and max_value")
        request = {"setDataValidation": {
            "range": gr,
            "rule": {
                "condition": {
                    "type": "NUMBER_BETWEEN",
                    "values": [{"userEnteredValue": str(min_value)}, {"userEnteredValue": str(max_value)}],
                },
                "strict": strict,
            },
        }}
    elif kind == "checkbox":
        request = {"setDataValidation": {
            "range": gr,
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }}
    else:
        raise ValueError(f"unknown validation kind {kind!r}")

    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [request]},
    ).execute()
    return {"ok": True, "range": range, "kind": kind}


def set_conditional_format(
    spreadsheet_id: str,
    range: str,
    condition: str,
    color: dict | None = None,
    threshold: float | None = None,
    text: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Add a conditional-format rule.

    `condition` shortcuts:
      - "negatives_red" — values < 0 → red background.
      - "positives_green" — values > 0 → green background.
      - "less_than" — needs `threshold`; cells < threshold get `color`.
      - "greater_than" — needs `threshold`.
      - "text_contains" — needs `text`; cells containing the substring get `color`.

    Default colors: red = {"red": 0.96, "green": 0.80, "blue": 0.80};
    green = {"red": 0.82, "green": 0.93, "blue": 0.82}.
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)

    if condition == "negatives_red":
        cond_type, cond_values = "NUMBER_LESS", [{"userEnteredValue": "0"}]
        bg = color or {"red": 0.96, "green": 0.80, "blue": 0.80}
    elif condition == "positives_green":
        cond_type, cond_values = "NUMBER_GREATER", [{"userEnteredValue": "0"}]
        bg = color or {"red": 0.82, "green": 0.93, "blue": 0.82}
    elif condition == "less_than":
        if threshold is None:
            raise ValueError("less_than needs `threshold`")
        cond_type, cond_values = "NUMBER_LESS", [{"userEnteredValue": str(threshold)}]
        bg = color or {"red": 0.96, "green": 0.80, "blue": 0.80}
    elif condition == "greater_than":
        if threshold is None:
            raise ValueError("greater_than needs `threshold`")
        cond_type, cond_values = "NUMBER_GREATER", [{"userEnteredValue": str(threshold)}]
        bg = color or {"red": 0.82, "green": 0.93, "blue": 0.82}
    elif condition == "text_contains":
        if not text:
            raise ValueError("text_contains needs `text`")
        cond_type, cond_values = "TEXT_CONTAINS", [{"userEnteredValue": text}]
        bg = color or {"red": 1.0, "green": 0.95, "blue": 0.65}
    else:
        raise ValueError(f"unknown condition {condition!r}")

    rule = {
        "ranges": [gr],
        "booleanRule": {
            "condition": {"type": cond_type, "values": cond_values},
            "format": {"backgroundColor": bg},
        },
    }
    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addConditionalFormatRule": {"rule": rule, "index": 0}}]},
    ).execute()
    return {"ok": True, "range": range, "condition": condition, "color_applied": bg, "raw": resp}


_CHART_TYPE_MAP = {
    "line": "LINE",
    "bar": "BAR",
    "column": "COLUMN",
    "pie": "PIE",
    "area": "AREA",
    "scatter": "SCATTER",
}


def create_chart(
    spreadsheet_id: str,
    sheet: str | int,
    chart_type: str,
    title: str,
    domain_range: str,
    series_ranges: list[str],
    position_sheet: str | int | None = None,
    position_row: int = 0,
    position_col: int = 0,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Insert a chart on `sheet`. Returns {chart_id}.

    `chart_type`: line/bar/column/pie/area/scatter.
    `domain_range` = A1 range for the X axis (one column).
    `series_ranges` = list of A1 ranges, one per Y series.
    `position_sheet`/row/col control where the chart is anchored (defaults
    to the same sheet at top-left).
    """
    if chart_type not in _CHART_TYPE_MAP:
        raise ValueError(f"unknown chart_type {chart_type!r}; allowed: {sorted(_CHART_TYPE_MAP)}")
    api_type = _CHART_TYPE_MAP[chart_type]
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet, account)
    pos_sheet_id = _resolve_sheet_id(spreadsheet_id, position_sheet, account) if position_sheet is not None else sheet_id

    def _range_to_grid(rng: str) -> dict:
        s, c = _split_a1(rng)
        sid = _resolve_sheet_id(spreadsheet_id, s, account)
        return _build_grid_range(sid, c)

    spec: dict
    if api_type == "PIE":
        spec = {
            "title": title,
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "domain": {"sourceRange": {"sources": [_range_to_grid(domain_range)]}},
                "series": {"sourceRange": {"sources": [_range_to_grid(series_ranges[0])]}},
                "threeDimensional": False,
            },
        }
    else:
        spec = {
            "title": title,
            "basicChart": {
                "chartType": api_type,
                "legendPosition": "RIGHT_LEGEND",
                "axis": [
                    {"position": "BOTTOM_AXIS"},
                    {"position": "LEFT_AXIS"},
                ],
                "domains": [{"domain": {"sourceRange": {"sources": [_range_to_grid(domain_range)]}}}],
                "series": [
                    {"series": {"sourceRange": {"sources": [_range_to_grid(rng)]}}, "targetAxis": "LEFT_AXIS"}
                    for rng in series_ranges
                ],
                "headerCount": 1,
            },
        }

    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addChart": {"chart": {
            "spec": spec,
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": pos_sheet_id, "rowIndex": position_row, "columnIndex": position_col},
                "offsetXPixels": 0, "offsetYPixels": 0,
                "widthPixels": 600, "heightPixels": 400,
            }},
        }}}]},
    ).execute()
    chart_id = resp.get("replies", [{}])[0].get("addChart", {}).get("chart", {}).get("chartId")
    return {"ok": True, "chart_id": chart_id, "chart_type": chart_type, "title": title}


def create_pivot(
    spreadsheet_id: str,
    source_range: str,
    rows: list[str],
    columns: list[str] | None = None,
    values: list[dict] | None = None,
    dest_sheet: str | int | None = None,
    dest_cell: str = "A1",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a pivot table.

    `source_range` = A1 like "Sales!A1:F1000". First row is treated as headers.
    `rows` / `columns` = list of column-header NAMES from the source.
    `values` = list of {column: <header_name>, aggregate: 'SUM'|'AVERAGE'|'COUNT'|...}.

    If `dest_sheet` omitted, creates a new tab named "Pivot-<source>".
    """
    src_sheet, src_cells = _split_a1(source_range)
    src_sheet_id = _resolve_sheet_id(spreadsheet_id, src_sheet, account)
    src_grid = _build_grid_range(src_sheet_id, src_cells)

    # Read headers to map name → column index
    header_resp = _service(account).spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{src_sheet}'!{src_cells.split(':')[0]}:{src_cells.split(':')[-1]}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    header_row = (header_resp.get("values") or [[]])[0]
    if not header_row:
        raise ValueError("source range has no headers in row 1")
    name_to_col = {str(h).strip(): i + src_grid.get("startColumnIndex", 0) for i, h in enumerate(header_row)}

    def _resolve(name: str) -> int:
        if name not in name_to_col:
            raise ValueError(f"header {name!r} not found; available: {list(name_to_col)}")
        return name_to_col[name]

    pivot_rows = [{"sourceColumnOffset": _resolve(r) - src_grid.get("startColumnIndex", 0), "showTotals": True, "sortOrder": "ASCENDING"} for r in rows]
    pivot_cols = [{"sourceColumnOffset": _resolve(c) - src_grid.get("startColumnIndex", 0), "showTotals": True, "sortOrder": "ASCENDING"} for c in (columns or [])]
    pivot_values = []
    for v in (values or []):
        pivot_values.append({
            "summarizeFunction": v.get("aggregate", "SUM"),
            "sourceColumnOffset": _resolve(v["column"]) - src_grid.get("startColumnIndex", 0),
            "name": v.get("name", f"{v.get('aggregate', 'SUM')} of {v['column']}"),
        })
    pivot = {
        "source": src_grid,
        "rows": pivot_rows,
        "columns": pivot_cols,
        "values": pivot_values,
        "valueLayout": "HORIZONTAL",
    }

    svc = _service(account)
    if dest_sheet is None:
        # Create a new tab and put pivot in A1
        new_tab_title = f"Pivot-{src_sheet}-{datetime.now().strftime('%H%M%S')}"
        add_resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": new_tab_title}}}]},
        ).execute()
        dest_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        dest_cell_grid = {"sheetId": dest_sheet_id, "rowIndex": 0, "columnIndex": 0}
    else:
        dest_sheet_id = _resolve_sheet_id(spreadsheet_id, dest_sheet, account)
        r, c = _a1_cell_to_indices(dest_cell)
        dest_cell_grid = {"sheetId": dest_sheet_id, "rowIndex": r, "columnIndex": c}

    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "updateCells": {
                "rows": [{"values": [{"pivotTable": pivot}]}],
                "start": dest_cell_grid,
                "fields": "pivotTable",
            },
        }]},
    ).execute()
    return {"ok": True, "dest_sheet_id": dest_sheet_id, "dest_cell": dest_cell}


# -------- Phase 3: collaboration (protected ranges + cell notes) --------

def add_protected_range(
    spreadsheet_id: str,
    range: str,
    description: str | None = None,
    warning_only: bool = False,
    editors: list[str] | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Protect a range from accidental edits.

    `warning_only=True` shows a confirm-prompt but lets editors proceed.
    `warning_only=False` (default) blocks anyone not in `editors` from
    writing. If `editors` is None, only the owner (you) can edit.

    Use for «зафиксируй формулы в столбце Год факт».
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)

    protected: dict = {"range": gr, "warningOnly": warning_only}
    if description:
        protected["description"] = description
    if not warning_only and editors:
        protected["editors"] = {"users": editors}

    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addProtectedRange": {"protectedRange": protected}}]},
    ).execute()
    pr = resp.get("replies", [{}])[0].get("addProtectedRange", {}).get("protectedRange", {})
    return {
        "ok": True,
        "protected_range_id": pr.get("protectedRangeId"),
        "range": range,
        "warning_only": warning_only,
    }


def list_protected_ranges(spreadsheet_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List every protected range in a spreadsheet. Returns {protected_ranges, _meta}."""
    svc = _service(account)
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title),protectedRanges)",
    ).execute()
    title_by_id = {s["properties"]["sheetId"]: s["properties"]["title"] for s in meta.get("sheets", [])}
    out = []
    for s in meta.get("sheets", []):
        for pr in s.get("protectedRanges", []) or []:
            gr = pr.get("range", {})
            sheet_name = title_by_id.get(gr.get("sheetId"), f"sheet#{gr.get('sheetId')}")
            a1 = _grid_range_to_a1(sheet_name, gr) if gr.get("endRowIndex") else f"'{sheet_name}'"
            out.append({
                "protected_range_id": pr.get("protectedRangeId"),
                "description": pr.get("description"),
                "warning_only": pr.get("warningOnly", False),
                "sheet": sheet_name,
                "range": a1,
                "editors": (pr.get("editors") or {}).get("users", []),
            })
    return {
        "protected_ranges": out,
        "_meta": {
            "count": len(out),
            "empty_reason": None if out else "no_protected_ranges",
        },
    }


def remove_protected_range(
    spreadsheet_id: str,
    protected_range_id: int,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Remove a protected range by its numeric protectedRangeId."""
    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteProtectedRange": {"protectedRangeId": protected_range_id}}]},
    ).execute()
    return {"ok": True, "protected_range_id": protected_range_id}


def set_cell_note(
    spreadsheet_id: str,
    range: str,
    note: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Attach a note (the hover-shown 'комментарий к ячейке') to a cell or range.

    Distinct from Drive comments (which are file-level discussions). Sheets
    notes are pinned to specific cells and visible by hovering. Use for
    «оставь заметку: проверить с бухгалтером» on a particular cell.

    To clear, pass note="" (empty string).
    """
    sheet_part, cell_part = _split_a1(range)
    sheet_id = _resolve_sheet_id(spreadsheet_id, sheet_part, account)
    gr = _build_grid_range(sheet_id, cell_part)

    _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": gr,
                "cell": {"note": note},
                "fields": "note",
            },
        }]},
    ).execute()
    return {"ok": True, "range": range, "note_length": len(note)}


def get_cell_notes(
    spreadsheet_id: str,
    range: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read notes attached to cells in a range. Returns {notes: [[note_or_None, ...]], _meta}.

    Result is a 2D array matching the requested range, with each entry
    being the note string (or None if no note set). Backed by
    `spreadsheets.get(includeGridData=true, fields=...note)`.
    """
    resp = _service(account).spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[range],
        includeGridData=True,
        fields="sheets.data.rowData.values.note",
    ).execute()
    notes_grid: list[list] = []
    total_with_note = 0
    for s in resp.get("sheets", []):
        for data in s.get("data", []):
            for row in data.get("rowData", []):
                row_notes = []
                for cell in row.get("values", []) or []:
                    n = cell.get("note")
                    row_notes.append(n)
                    if n:
                        total_with_note += 1
                notes_grid.append(row_notes)
    return {
        "notes": notes_grid,
        "_meta": {
            "range_read": range,
            "rows": len(notes_grid),
            "non_empty_count": total_with_note,
            "empty_reason": None if total_with_note else "no_notes",
        },
    }


def write_range(spreadsheet_id: str, range: str, values: list[list], dry_run: bool = False, account: str = DEFAULT_ACCOUNT) -> dict:
    """Overwrite cells in `range` with `values`. Auto-snapshots first
    (recoverable via sheets_rollback). With `dry_run=True` returns a
    preview ({would_write_cells, shape, values_sample, current_first_3_rows,
    reversibility}) and does NOT touch the spreadsheet."""
    if dry_run:
        rows = len(values)
        cols = max((len(r) for r in values), default=0)
        total_cells = sum(len(r) for r in values)
        current_sample = None
        try:
            current = _service(account).spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range,
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
            current_sample = (current.get("values", []) or [])[:3]
        except Exception:
            pass
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "sheets.spreadsheets.values.update",
                "spreadsheet_id": spreadsheet_id,
                "range": range,
                "would_write_cells": total_cells,
                "shape": {"rows": rows, "cols": cols},
                "values_sample": values[:3],
                "current_first_3_rows": current_sample,
                "reversibility": (
                    "REVERSIBLE via sheets_list_backups + sheets_rollback. "
                    "Each write_range auto-snapshots the affected range to "
                    ".data/sheets_backups/<spreadsheet_id>/<ts>.json."
                ),
            },
            "_meta": {"native_preview": True},
        }
    snap = _snapshot(spreadsheet_id, range, account, "write_range")
    result = _service(account).spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    _invalidate_read_cache(spreadsheet_id)
    if snap:
        result["snapshot_id"] = snap
    return result


def append_rows(spreadsheet_id: str, range: str, values: list[list], account: str = DEFAULT_ACCOUNT) -> dict:
    # No snapshot for append — pure addition, undoable by deleting the new rows
    result = _service(account).spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    _invalidate_read_cache(spreadsheet_id)
    return result


def clear_range(spreadsheet_id: str, range: str, dry_run: bool = False, account: str = DEFAULT_ACCOUNT) -> dict:
    """Clear cell VALUES in `range` (formatting preserved). Auto-snapshots
    first — recoverable via sheets_rollback. With `dry_run=True` returns
    a preview of how many cells carry data and a sample of what would be
    cleared, WITHOUT touching the spreadsheet."""
    if dry_run:
        current_sample = None
        non_empty = None
        try:
            current = _service(account).spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range,
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
            rows = current.get("values", []) or []
            current_sample = rows[:3]
            non_empty = sum(1 for r in rows for v in r if v not in (None, ""))
        except Exception:
            pass
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "sheets.spreadsheets.values.clear",
                "spreadsheet_id": spreadsheet_id,
                "range": range,
                "non_empty_cells": non_empty,
                "current_first_3_rows": current_sample,
                "reversibility": (
                    "REVERSIBLE — clear_range auto-snapshots to "
                    ".data/sheets_backups/<spreadsheet_id>/<ts>.json before "
                    "clearing. Restore via sheets_rollback."
                ),
            },
            "_meta": {"native_preview": True},
        }
    snap = _snapshot(spreadsheet_id, range, account, "clear_range")
    result = _service(account).spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range, body={}
    ).execute()
    _invalidate_read_cache(spreadsheet_id)
    if snap:
        result["snapshot_id"] = snap
    return result


def list_backups(spreadsheet_id: str, limit: int = 20) -> list[dict]:
    """Return recent snapshots for one spreadsheet, newest first."""
    sheet_dir = BACKUPS_DIR / spreadsheet_id
    if not sheet_dir.exists():
        return []
    files = sorted(sheet_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "snapshot_id": data.get("snapshot_id", p.stem),
            "ts": data.get("ts"),
            "range": data.get("range"),
            "op": data.get("op"),
            "row_count": len(data.get("values", []) or []),
        })
    return out


def rollback(spreadsheet_id: str, snapshot_id: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    """Restore a previous snapshot. If snapshot_id omitted, uses the most recent."""
    sheet_dir = BACKUPS_DIR / spreadsheet_id
    if not sheet_dir.exists():
        raise FileNotFoundError(f"no backups for {spreadsheet_id}")
    if snapshot_id is None:
        files = sorted(sheet_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise FileNotFoundError(f"no backups for {spreadsheet_id}")
        path = files[0]
    else:
        path = sheet_dir / f"{snapshot_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"snapshot {snapshot_id} not found for {spreadsheet_id}")

    data = json.loads(path.read_text(encoding="utf-8"))
    range_a1 = data["range"]
    values = data.get("values", []) or []

    # Restore: first clear (to remove rows the snapshot didn't cover),
    # then write the captured values.
    svc = _service(account)
    svc.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range_a1, body={}
    ).execute()
    if values:
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    _invalidate_read_cache(spreadsheet_id)
    return {
        "restored_from": data["snapshot_id"],
        "range": range_a1,
        "rows_restored": len(values),
        "ts_of_snapshot": data.get("ts"),
    }


def excel_to_sheets(
    local_path: str,
    title: str | None = None,
    parent_folder_id: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """One-shot macro: parse a local .xlsx, create a new Google Spreadsheet,
    move it into `parent_folder_id` (optional), and copy every workbook sheet
    into a same-named Google sheet. Returns spreadsheet_id, url, and a list
    of sheets created with row counts.
    """
    from src.tools import drive as _drive, excel as _excel  # local to avoid cycles

    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(local_path)

    parsed = _excel.parse_xlsx(str(path))
    if not isinstance(parsed, dict):
        raise TypeError("expected multi-sheet workbook")

    target_title = title or path.stem
    svc = _service(account)
    created = svc.spreadsheets().create(
        body={"properties": {"title": target_title}},
        fields="spreadsheetId,spreadsheetUrl,sheets.properties",
    ).execute()
    sid = created["spreadsheetId"]
    url = created["spreadsheetUrl"]

    # New spreadsheet has a default "Sheet1" — we'll repurpose / replace.
    default_sheet_id = created["sheets"][0]["properties"]["sheetId"]
    default_sheet_name = created["sheets"][0]["properties"]["title"]

    workbook_sheets = list(parsed.keys())
    requests = []
    for idx, name in enumerate(workbook_sheets):
        if idx == 0:
            # rename default to first workbook sheet
            if name != default_sheet_name:
                requests.append({
                    "updateSheetProperties": {
                        "properties": {"sheetId": default_sheet_id, "title": name},
                        "fields": "title",
                    }
                })
        else:
            requests.append({"addSheet": {"properties": {"title": name}}})
    if requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()

    # Optional move to a folder
    if parent_folder_id:
        try:
            _drive.move(file_id=sid, new_parent_id=parent_folder_id, account=account)
        except Exception:
            pass  # creation succeeded; move is best-effort

    # Write rows for each sheet
    written = []
    for sheet_name, rows in parsed.items():
        if not rows:
            written.append({"sheet": sheet_name, "rows": 0})
            continue
        headers = list(rows[0].keys())
        values = [headers] + [[r.get(h) for h in headers] for r in rows]
        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        written.append({"sheet": sheet_name, "rows": len(rows)})

    return {
        "spreadsheet_id": sid,
        "url": url,
        "title": target_title,
        "parent_folder_id": parent_folder_id,
        "sheets": written,
    }


def find_and_replace(
    spreadsheet_id: str,
    find: str,
    replace: str,
    sheet: str | None = None,
    match_case: bool = False,
    match_entire_cell: bool = False,
    use_regex: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Sheets-native find-and-replace via batchUpdate. One call, no read+write
    cycle. If `sheet` given, scope to that tab; otherwise all sheets.
    Snapshot-backed: takes a backup of the affected scope first.
    """
    svc = _service(account)

    # Take a backup snapshot of either the whole sheet or every sheet
    if sheet:
        _snapshot(spreadsheet_id, sheet, account, "find_and_replace")
    else:
        meta = svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties.title"
        ).execute()
        for s in meta.get("sheets", []):
            _snapshot(spreadsheet_id, s["properties"]["title"], account, "find_and_replace")

    request = {
        "findReplace": {
            "find": find,
            "replacement": replace,
            "matchCase": match_case,
            "matchEntireCell": match_entire_cell,
            "searchByRegex": use_regex,
            "allSheets": sheet is None,
        }
    }
    if sheet is not None:
        meta = svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties"
        ).execute()
        sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == sheet:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            raise ValueError(f"sheet {sheet!r} not found in spreadsheet")
        request["findReplace"]["sheetId"] = sheet_id
        del request["findReplace"]["allSheets"]

    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [request]}
    ).execute()
    fr = resp.get("replies", [{}])[0].get("findReplaceResponse", {})
    _invalidate_read_cache(spreadsheet_id)
    return {
        "occurrences_changed": fr.get("occurrencesChanged", 0),
        "values_changed": fr.get("valuesChanged", 0),
        "rows_changed": fr.get("rowsChanged", 0),
        "sheets_changed": fr.get("sheetsChanged", 0),
        "formulas_changed": fr.get("formulasChanged", 0),
    }


def create_spreadsheet(title: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    ).execute()


def add_sheet(spreadsheet_id: str, title: str, account: str = DEFAULT_ACCOUNT) -> dict:
    resp = _service(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]


def get_metadata(spreadsheet_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetId,properties.title,sheets.properties",
    ).execute()


def _col_to_a1(col_0: int) -> str:
    out = ""
    n = col_0
    while True:
        out = chr(ord("A") + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def _quote_sheet_name(sheet: str) -> str:
    """Wrap a sheet name in single quotes so spaces / Cyrillic / leading digits
    don't break the Sheets API A1 parser. Embedded single quotes are doubled
    per Google's spec (e.g. ``Lena's data`` → ``'Lena''s data'``)."""
    return "'" + sheet.replace("'", "''") + "'"


def _invalidate_read_cache(spreadsheet_id: str) -> None:
    """Drop any cached reads of this spreadsheet — a mutating op just made
    them stale. No-op when SHEETS_READ_CACHE=0 (default). Phase 14E."""
    from src.tools._read_cache import invalidate as _cache_invalidate
    _cache_invalidate(spreadsheet_id)


def summarize(spreadsheet_id: str, sample_rows: int = 5, account: str = DEFAULT_ACCOUNT) -> dict:
    """Single-call structural summary of a spreadsheet: title, every sheet's
    name + grid size + header row + first `sample_rows` data rows. Lets the
    agent understand what's in a sheet without reading the whole thing.
    """
    svc = _service(account)
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetId,properties.title,sheets.properties",
    ).execute()

    out = {
        "spreadsheet_id": meta["spreadsheetId"],
        "title": meta["properties"]["title"],
        "sheets": [],
    }

    sample_n = max(1, min(sample_rows, 50))
    for s in meta.get("sheets", []):
        props = s["properties"]
        sheet_name = props["title"]
        grid = props.get("gridProperties", {})
        rows_total = grid.get("rowCount", 0)
        cols_total = grid.get("columnCount", 0)

        # A1:ZZ (not A1:Z) — Z caps at 26 cols and silently drops everything
        # further right on wider sheets.
        rng = f"'{sheet_name}'!A1:ZZ{1 + sample_n}"
        try:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=rng
            ).execute()
            values = resp.get("values", [])
        except Exception as e:
            values = []
            err = f"{type(e).__name__}: {e}"
        else:
            err = None

        header = values[0] if values else []
        sample = values[1:] if len(values) > 1 else []

        # Actual data extent (column A) — grid.rows is the sheet DIMENSION
        # and is usually padded with blank trailing rows.
        data_rows_estimate = None
        try:
            ldr = last_data_row(spreadsheet_id, sheet_name, "A", account=account)
            data_rows_estimate = ldr.get("last_row", 0)
        except Exception:
            pass

        cols_in_sample = max((len(r) for r in values), default=0)
        out["sheets"].append({
            "name": sheet_name,
            "grid": {"rows": rows_total, "cols": cols_total},
            "header": header,
            "sample_rows": sample,
            "read_error": err,
            "_meta": {
                "range_read": rng,
                "data_rows_estimate": data_rows_estimate,
                "sample_size": len(sample),
                "is_sample": (data_rows_estimate or 0) > len(sample),
                "cols_in_sample": cols_in_sample,
                "truncated_columns": cols_in_sample < cols_total and cols_total > 0,
            },
        })

    return out


def query(
    spreadsheet_id: str,
    source_range: str,
    sql: str,
    response_format: str = "concise",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Run a Google QUERY against a range in this spreadsheet — server-side
    aggregation that scales to millions of rows. Creates a TEMPORARY sheet
    inside the same file, writes the QUERY formula, reads the result, then
    deletes the temp sheet.

    `sql` uses Google's QUERY language: SELECT, WHERE, GROUP BY, ORDER BY,
    LIMIT. Columns are addressed positionally as Col1, Col2, ... or by letter
    A, B, ... depending on whether source_range has a header. We use header=1
    (treat first row of source_range as header).

    `response_format`:
      - "concise" (default): returns first 50 rows + total row_count.
        Saves tokens on large aggregates where the agent only needs the
        top of the result to formulate a reply. Caller can re-call with
        "detailed" if it actually needs the full grid.
      - "detailed": all rows up to the 10000-row read cap.
    """
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    svc = _service(account)
    ts = datetime.now().strftime("%H%M%S%f")[:-3]
    temp_name = f"_agent_q_{ts}"

    add_resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": temp_name, "hidden": True}}}]},
    ).execute()
    temp_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    try:
        sep = _arg_sep(spreadsheet_id, account)
        escaped_sql = sql.replace('"', '""')
        formula = f'=QUERY({source_range}{sep} "{escaped_sql}"{sep} 1)'
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()

        resp = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1:ZZ10000",
        ).execute()
        values = resp.get("values", []) or []
        # If we got back exactly 10000 rows, the QUERY likely produced MORE
        # and we read up to our cap. Flag it so the agent can't claim
        # completeness.
        hit_read_cap = len(values) >= 10000

        # trim trailing empty rows / cols
        while values and not any(c not in (None, "") for c in (values[-1] or [])):
            values.pop()

        if values and values[0] and isinstance(values[0][0], str) and values[0][0].startswith("#"):
            return {
                "error": "; ".join(str(c) for c in values[0] if c),
                "rows": [],
                "row_count": 0,
                "_meta": {"empty_reason": "query_error", "truncated": False},
            }

        total = len(values)
        if response_format == "concise" and total > 50:
            rows_out = values[:50]
            concise_truncated = True
        else:
            rows_out = values
            concise_truncated = False
        return {
            "rows": rows_out,
            "row_count": total,
            "_meta": {
                "truncated": hit_read_cap or concise_truncated,
                "truncation_reason": (
                    "hit 10000-row read cap; rerun with a tighter WHERE clause or LIMIT"
                    if hit_read_cap
                    else (
                        f"response_format='concise' returned 50/{total} rows; "
                        "call again with response_format='detailed' for the full grid"
                        if concise_truncated else None
                    )
                ),
                "response_format": response_format,
                "shown_rows": len(rows_out),
                "empty_reason": None if values else "no_matches",
            },
        }
    finally:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": temp_sheet_id}}]},
            ).execute()
        except Exception:
            pass


def profile(spreadsheet_id: str, sheet: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Column-by-column statistics for a sheet — uses Google formulas so it
    runs on the server regardless of file size. Returns per column:
    name, non_blank, blank, distinct, top_5 (most frequent values), and for
    numeric columns also min/max/avg/sum. NO raw row data is fetched.
    """
    svc = _service(account)

    # Read just the header + 5 rows to detect numeric columns
    sample = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{sheet}'!A1:ZZ6",
    ).execute().get("values", []) or []
    if not sample:
        return {"sheet": sheet, "columns": []}
    headers = sample[0]
    n_cols = len(headers)

    def col_letter(i: int) -> str:
        s = ""
        n = i
        while True:
            s = chr(ord("A") + n % 26) + s
            n = n // 26 - 1
            if n < 0:
                return s

    # Detect numeric: any non-empty value in first 5 data rows that looks numeric
    def is_numeric(idx: int) -> bool:
        for r in sample[1:]:
            if idx < len(r) and r[idx] not in (None, ""):
                v = str(r[idx]).replace(",", ".").replace(" ", "").replace("\xa0", "")
                try:
                    float(v); return True
                except ValueError:
                    return False
        return False

    ts = datetime.now().strftime("%H%M%S%f")[:-3]
    temp_name = f"_agent_p_{ts}"
    add_resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": temp_name, "hidden": True}}}]},
    ).execute()
    temp_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    try:
        sep = _arg_sep(spreadsheet_id, account)
        out_values: list[list] = [[None] * n_cols for _ in range(13)]
        numeric_flags = []
        for i in range(n_cols):
            letter = col_letter(i)
            full = f"'{sheet}'!{letter}2:{letter}"
            numeric = is_numeric(i)
            numeric_flags.append(numeric)

            out_values[0][i] = headers[i] or letter
            out_values[1][i] = f"=COUNTA({full})"
            out_values[2][i] = f"=COUNTBLANK({full})"
            out_values[3][i] = f"=COUNTUNIQUE({full})"
            out_values[4][i] = "numeric" if numeric else "text"
            if numeric:
                out_values[5][i] = f"=IFERROR(MIN({full}){sep})"
                out_values[6][i] = f"=IFERROR(MAX({full}){sep})"
                out_values[7][i] = f"=IFERROR(AVERAGE({full}){sep})"
            # Top 5 via QUERY — value+count joined into one cell
            out_values[8][i] = (
                f'=IFERROR(JOIN(" | "{sep} QUERY({full}{sep} '
                f'"SELECT {letter}, COUNT({letter}) WHERE {letter} IS NOT NULL '
                f'GROUP BY {letter} ORDER BY COUNT({letter}) DESC LIMIT 5 LABEL {letter} \'\', '
                f'COUNT({letter}) \'\'"{sep} 0)){sep})'
            )

        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": out_values},
        ).execute()

        # Read back
        last_col = col_letter(max(0, n_cols - 1))
        result = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1:{last_col}13",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])

        # Stitch into per-column dict
        def cell(r, c):
            row = result[r] if r < len(result) else []
            return row[c] if c < len(row) else None

        cols_out = []
        for i in range(n_cols):
            entry = {
                "name": cell(0, i),
                "non_blank": cell(1, i),
                "blank": cell(2, i),
                "distinct": cell(3, i),
                "type": cell(4, i),
                "top_5": cell(8, i),
            }
            if numeric_flags[i]:
                entry["min"] = cell(5, i)
                entry["max"] = cell(6, i)
                entry["avg"] = cell(7, i)
            cols_out.append(entry)
        return {"sheet": sheet, "columns": cols_out}
    finally:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": temp_sheet_id}}]},
            ).execute()
        except Exception:
            pass


def iter_rows(
    spreadsheet_id: str,
    sheet: str,
    offset: int = 0,
    chunk_size: int = 200,
    columns: str = "A:ZZ",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read `chunk_size` rows of `columns` starting AT 0-based `offset` (data
    row offset — header is at row 1 if any; offset=0 means start of data).
    Returns {rows, offset, next_offset, has_more}. Use for paged traversal
    of huge sheets when you genuinely need per-row inspection.

    Pair with offset feedback loop: call with offset=0, then offset=next_offset
    until has_more=False.
    """
    if "!" in sheet or ":" in sheet:
        raise ValueError("`sheet` must be just the tab name, not a range")
    cs = max(1, min(chunk_size, 5000))
    start = offset + 2  # +1 because rows are 1-indexed, +1 to skip header
    end = start + cs - 1
    # columns "A:ZZ" → "A{start}:ZZ{end}"
    if ":" in columns:
        left, right = columns.split(":")
        cell_range = f"{left}{start}:{right}{end}"
    else:
        cell_range = f"{columns}{start}:{columns}{end}"

    resp = _service(account).spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet}'!{cell_range}",
    ).execute()
    rows = resp.get("values", []) or []

    has_more = len(rows) >= cs
    next_offset = offset + len(rows) if has_more else None
    return {
        "offset": offset,
        "rows": rows,
        "row_count": len(rows),
        "next_offset": next_offset,
        "has_more": has_more,
        "_meta": {
            "range_read": resp.get("range", f"'{sheet}'!{cell_range}"),
            "truncated": has_more,
            "truncation_reason": (
                f"chunk_size={cs} reached; call again with offset={next_offset}"
                if has_more else None
            ),
            "empty_reason": None if rows else "no_data",
        },
    }


def find_in_spreadsheet(
    spreadsheet_id: str,
    query: str,
    case_sensitive: bool = False,
    with_labels: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Search for a substring across ALL sheets in one spreadsheet.

    Returns {matches, _meta}. Each match has sheet name, A1 cell,
    row/col indices, and value. When `with_labels=True`, each match also
    carries `row_label` (col A of the match row) and `col_label` (row 1 of
    the match column) — use this to verify a number's meaning before
    quoting it (e.g., "Чистая прибыль" / "Год факт"). Skips the labels
    themselves so the result doesn't echo your search term.
    """
    svc = _service(account)
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()

    needle = query if case_sensitive else query.lower()
    matches: list[dict] = []
    for s in meta.get("sheets", []):
        sheet_name = s["properties"]["title"]
        sheet_a1 = _quote_sheet_name(sheet_name)
        try:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=sheet_a1
            ).execute()
            rows = resp.get("values", [])
        except Exception:
            continue

        header_row = rows[0] if rows else []
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                hay = str(val)
                if not case_sensitive:
                    hay = hay.lower()
                if needle in hay:
                    entry = {
                        "sheet": sheet_name,
                        "cell": f"{sheet_a1}!{_col_to_a1(c_idx)}{r_idx + 1}",
                        "row": r_idx + 1,
                        "col": c_idx + 1,
                        "value": val,
                    }
                    if with_labels:
                        # row_label = col A of this row (unless the match
                        # IS in col A); col_label = row 1 of this column
                        # (unless the match IS in row 1).
                        row_label = None
                        if c_idx != 0 and r_idx < len(rows):
                            row_a = rows[r_idx]
                            row_label = row_a[0] if row_a else None
                        col_label = None
                        if r_idx != 0 and c_idx < len(header_row):
                            col_label = header_row[c_idx]
                        entry["row_label"] = row_label
                        entry["col_label"] = col_label
                    matches.append(entry)
    return {
        "matches": matches,
        "_meta": {
            "match_count": len(matches),
            "empty_reason": None if matches else "no_matches",
            "with_labels": with_labels,
        },
    }


def last_data_row(
    spreadsheet_id: str,
    sheet: str,
    column: str = "A",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Find the last non-empty row in `column` of `sheet`. Useful because
    sheets_summarize().grid.rows is the SHEET DIMENSION (often inflated with
    blank trailing rows), not the actual data extent.

    Returns {last_row, value, sheet, column}. last_row=0 means column is empty.
    """
    svc = _service(account)
    rng = f"{_quote_sheet_name(sheet)}!{column}1:{column}"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rng,
        majorDimension="ROWS",
    ).execute()
    vals = resp.get("values", [])
    last_row = 0
    last_value = None
    for i, row in enumerate(vals, 1):
        if row and row[0] != "":
            last_row = i
            last_value = row[0]
    return {
        "sheet": sheet,
        "column": column,
        "last_row": last_row,
        "value": last_value,
        "total_rows_scanned": len(vals),
    }


def snapshot_range(
    spreadsheet_id: str,
    range: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Take a structural snapshot of `range`: every cell value + last-row
    indicator. Cheap (one read) — keep it in memory or pass between operations.
    Returns {range, values, rows, cols, taken_at}.

    Pair with sheets_diff_snapshot(before, after) to see what changed after
    a write or external execution.
    """
    import datetime as _dt
    values = read_range(spreadsheet_id, range, account=account)["values"]
    rows = len(values)
    cols = max((len(r) for r in values), default=0)
    return {
        "spreadsheet_id": spreadsheet_id,
        "range": range,
        "values": values,
        "rows": rows,
        "cols": cols,
        "taken_at": _dt.datetime.utcnow().isoformat() + "Z",
    }


def metric_lookup(
    spreadsheet_id: str,
    metric: str,
    period: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """One-shot resolver for «найди значение метрики X за период Y».

    Tries strategies in order and short-circuits on the first hit:
      1. `list_named_ranges` + fuzzy match name to `metric` → if hit, read it.
         (Skipped when `period` is set — named ranges typically encode one
         specific period, so the safer path is via labels.)
      2. `find_in_spreadsheet(metric, with_labels=True)` → if exactly one
         match, AND (period is None OR `col_label` fuzzy-matches `period`)
         → return it.
      3. Multiple matches → filter by `period` against `col_label` and
         `row_label`. If exactly one remains, return.
      4. Nothing matched → return strategy=None with `candidates` listing
         the closest finds so the agent can pick or ask the user.

    Returns:
        {
          "value": ...,
          "cell": "Год факт!B45",
          "row_label": "Чистая прибыль",
          "col_label": "Год",
          "_meta": {
            "strategy": "named_range" | "find_with_labels" | "period_filter" | None,
            "confidence": "high" | "medium" | "low",
            "candidates_seen": N,
            "candidates": [...],   # only when strategy=None
          }
        }
    """
    metric_norm = metric.strip().lower()
    period_norm = period.strip().lower() if period else None

    # ----- Strategy 1: named ranges (only when no period specified) -----
    if period is None:
        try:
            nrs = list_named_ranges(spreadsheet_id, account=account)
            for nr in nrs.get("named_ranges", []) or []:
                name = (nr.get("name") or "")
                if _fuzzy_label_match(name, metric_norm):
                    rr = read_named_range(spreadsheet_id, name, account=account)
                    values = rr.get("values") or []
                    if values and values[0]:
                        return {
                            "value": values[0][0],
                            "cell": rr["_meta"].get("range_read"),
                            "row_label": None,
                            "col_label": None,
                            "_meta": {
                                "strategy": "named_range",
                                "named_range": name,
                                "confidence": "high",
                            },
                        }
        except Exception:
            pass  # fallthrough to find_with_labels

    # ----- Strategy 2 + 3: find_in_spreadsheet with labels -----
    candidates: list[dict] = []
    try:
        fis = find_in_spreadsheet(
            spreadsheet_id, metric,
            case_sensitive=False, with_labels=True, account=account,
        )
        matches = fis.get("matches", []) or []
    except Exception:
        matches = []

    # The `metric` token will most often APPEAR as a row_label (the metric
    # label itself sits in column A). To get the VALUE we want neighbouring
    # cells from that row. So we treat each text-cell match as a hint and
    # then read the row.
    label_rows: list[dict] = []  # list of {sheet, row, row_label, header_row}
    for m in matches:
        # If the matched cell IS the metric label (string match in col A),
        # surface its row for further reading.
        if isinstance(m.get("value"), str) and metric_norm in m["value"].lower():
            label_rows.append({
                "sheet": m["sheet"],
                "row": m["row"],
                "col": m["col"],
                "row_label": m["value"],
            })

    # If we have label_rows, read the period columns for those rows
    for lr in label_rows:
        try:
            period_match = _find_value_in_row(
                spreadsheet_id, lr["sheet"], lr["row"], period_norm, account,
            )
        except Exception:
            continue
        if period_match:
            candidates.append({
                "value": period_match["value"],
                "cell": period_match["cell"],
                "row_label": lr["row_label"],
                "col_label": period_match["col_label"],
            })

    if len(candidates) == 1:
        c = candidates[0]
        strategy = "find_with_labels" if period_norm is None else "period_filter"
        return {
            **c,
            "_meta": {
                "strategy": strategy,
                "confidence": "high",
                "candidates_seen": 1,
            },
        }
    if len(candidates) > 1:
        return {
            "value": None,
            "cell": None,
            "row_label": None,
            "col_label": None,
            "_meta": {
                "strategy": None,
                "confidence": "low",
                "candidates_seen": len(candidates),
                "candidates": candidates[:10],
                "reason": "multiple candidates — narrow `period` to disambiguate",
            },
        }

    return {
        "value": None,
        "cell": None,
        "row_label": None,
        "col_label": None,
        "_meta": {
            "strategy": None,
            "confidence": "low",
            "candidates_seen": 0,
            "candidates": [],
            "reason": f"no row whose label contains {metric!r}",
        },
    }


def _fuzzy_label_match(label: str, needle: str) -> bool:
    """Case-insensitive substring match with light normalization
    (lowercase + collapse underscores so `Chistaya_Pribyl` matches
    `Chistaya Pribyl`).

    Match is one-directional: needle must appear inside the label, not
    vice-versa. A bidirectional match silently returns ``Прибыль`` when the
    caller actually asked for ``Чистая прибыль`` — a different metric."""
    if not label or not needle:
        return False
    norm_label = label.lower().replace("_", " ").strip()
    norm_needle = needle.lower().replace("_", " ").strip()
    return norm_needle in norm_label


def _find_value_in_row(
    spreadsheet_id: str,
    sheet: str,
    row: int,
    period_norm: str | None,
    account: str,
) -> dict | None:
    """Given a metric row, find the cell whose column-header matches `period_norm`.
    If period_norm is None, returns the LAST non-empty cell in the row (typical
    "Год" / cumulative column).
    """
    # Read the full row + header row
    row_range = f"'{sheet}'!{row}:{row}"
    header_range = f"'{sheet}'!1:1"
    batched = batch_read(spreadsheet_id, [row_range, header_range], account=account)
    per = batched.get("per_range", [])
    if len(per) < 2:
        return None
    row_vals = (per[0].get("values") or [[]])[0] if per[0].get("values") else []
    header_vals = (per[1].get("values") or [[]])[0] if per[1].get("values") else []
    if not row_vals:
        return None

    # If period given, find matching header
    if period_norm:
        for c_idx, header in enumerate(header_vals):
            if header and period_norm in str(header).lower():
                if c_idx < len(row_vals):
                    val = row_vals[c_idx]
                    return {
                        "value": val,
                        "cell": f"'{sheet}'!{_col_to_a1(c_idx)}{row}",
                        "col_label": header,
                    }
        return None

    # No period → find LAST non-empty cell after column A
    for c_idx in range(len(row_vals) - 1, 0, -1):
        v = row_vals[c_idx]
        if v not in (None, ""):
            header = header_vals[c_idx] if c_idx < len(header_vals) else None
            return {
                "value": v,
                "cell": f"'{sheet}'!{_col_to_a1(c_idx)}{row}",
                "col_label": header,
            }
    return None


def write_and_verify(
    spreadsheet_id: str,
    range: str,
    values: list[list],
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """`write_range` + read-back verification.

    Steps: snapshot_range(before) → write_range(values) → read_range(after)
    → compare each cell to the expected write.

    `verdict`:
      - "ok"        : every written cell matches its expected value.
      - "modified"  : Sheets reformatted/evaluated some cells (e.g. formula
                      `=1+1` came back as `2`). discrepancies lists deltas.
      - "error"     : the write call itself failed (raised; not reached here).

    Returns {ok, verdict, range, before_snapshot_id, rows_written,
             discrepancies, _meta}.
    """
    # Step 1: structural snapshot for diff (write_range also auto-snapshots
    # to disk via _snapshot, this in-memory one is just for the verdict)
    try:
        before = snapshot_range(spreadsheet_id, range, account=account)
    except Exception as e:
        before = {"values": [], "error": str(e)}

    # Step 2: write (write_range invalidates the read cache itself)
    write_result = write_range(spreadsheet_id, range, values, account=account)

    # Step 3: read back what's actually in the cells
    try:
        after = read_range(spreadsheet_id, range, account=account)
        after_values = after.get("values", [])
    except Exception as e:
        return {
            "ok": False,
            "verdict": "error",
            "range": range,
            "before_snapshot_id": write_result.get("snapshot_id"),
            "error": f"verification read failed: {type(e).__name__}: {e}",
            "_meta": {"strategy": "write_and_verify"},
        }

    # Step 4: cell-by-cell diff
    discrepancies = []
    for r, expected_row in enumerate(values):
        actual_row = after_values[r] if r < len(after_values) else []
        for c, expected in enumerate(expected_row):
            actual = actual_row[c] if c < len(actual_row) else None
            # Normalize None vs "" — both treated as empty
            if (expected in (None, "")) and (actual in (None, "")):
                continue
            if _values_equal(actual, expected):
                continue
            discrepancies.append({
                "row": r + 1,
                "col": c + 1,
                "expected": expected,
                "actual": actual,
            })

    verdict = "ok" if not discrepancies else "modified"
    return {
        "ok": True,
        "verdict": verdict,
        "range": range,
        "rows_written": len(values),
        "before_snapshot_id": write_result.get("snapshot_id"),
        "updated_range": write_result.get("updatedRange"),
        "discrepancies": discrepancies[:50],  # cap
        "_meta": {
            "strategy": "write_and_verify",
            "discrepancy_count": len(discrepancies),
            "before_rows": len(before.get("values", [])),
            "after_rows": len(after_values),
        },
    }


def _values_equal(a, b) -> bool:
    """Loose equality: numerical coercion, string strip."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    return str(a).strip() == str(b).strip()


def run_formula(
    spreadsheet_id: str,
    formula: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Evaluate any Sheets formula (e.g. `=GOOGLEFINANCE("CURRENCY:USDRUB")`,
    `=IMPORTRANGE(...)`, `=YEAR(TODAY())`) WITHOUT creating a permanent cell.

    Uses the same temp-hidden-sheet pattern as `query()`. Returns
    {result, _meta:{formula, raw}}.

    `formula` must start with `=`.
    """
    if not formula.startswith("="):
        raise ValueError(f"formula must start with '=', got {formula!r}")
    svc = _service(account)
    ts = datetime.now().strftime("%H%M%S%f")[:-3]
    temp_name = f"_agent_f_{ts}"
    add_resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": temp_name, "hidden": True}}}]},
    ).execute()
    temp_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()
        # Read up to 200x10 in case the formula returns a range
        resp = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{temp_name}'!A1:J200",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        values = resp.get("values", []) or []
        # trim trailing empty rows/cols
        while values and not any(c not in (None, "") for c in (values[-1] or [])):
            values.pop()
        # If single cell, return scalar
        if len(values) == 1 and len(values[0]) == 1:
            scalar = values[0][0]
            is_error = isinstance(scalar, str) and scalar.startswith("#")
            return {
                "result": scalar,
                "_meta": {
                    "formula": formula,
                    "shape": "scalar",
                    "is_error": is_error,
                },
            }
        return {
            "result": values,
            "_meta": {
                "formula": formula,
                "shape": "range",
                "rows": len(values),
                "cols": max((len(r) for r in values), default=0),
            },
        }
    finally:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": temp_sheet_id}}]},
            ).execute()
        except Exception:
            pass


_MONTH_TOKENS = {
    "ru": ["январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
            "янв", "фев", "мар", "апр", "май", "июн",
            "июл", "авг", "сен", "окт", "ноя", "дек"],
    "en": ["january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec"],
}
_QUARTER_RE = __import__("re").compile(r"^q[1-4]$|^[1-4]q$|^кв\s*[1-4]$", __import__("re").IGNORECASE)
_YEAR_RE = __import__("re").compile(r"^(19|20)\d{2}$")
_PLAN_FACT_TOKENS = {"факт", "план", "fact", "plan", "actual", "budget"}


def period_detect(
    spreadsheet_id: str,
    sheet: str,
    header_row: int = 1,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Walk the header row of `sheet` and classify each column.

    Returns {periods: [{col, col_letter, label, kind, ...}], _meta}.
    `kind` is one of: "month", "quarter", "year", "plan_fact", "other".

    Heuristic only — meant to save the agent from guessing «какая
    колонка = декабрь 2025» on financial reports. Pair with sheets_read_range
    on the chosen column.
    """
    # Read just the header row
    rng = f"'{sheet}'!{header_row}:{header_row}"
    resp = _service(account).spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", []) or []
    headers = rows[0] if rows else []
    periods = []
    ru_months = set(_MONTH_TOKENS["ru"])
    en_months = set(_MONTH_TOKENS["en"])
    for idx, cell in enumerate(headers):
        col_letter = _col_to_a1(idx)
        label = str(cell or "").strip()
        if not label:
            continue
        low = label.lower()
        kind = "other"
        if low in ru_months or low in en_months:
            kind = "month"
        elif _QUARTER_RE.match(low):
            kind = "quarter"
        elif _YEAR_RE.match(low):
            kind = "year"
        elif any(t in low for t in _PLAN_FACT_TOKENS):
            kind = "plan_fact"
        # Year with month: "Янв 2026", "January 2026"
        parts = low.split()
        if len(parts) >= 2:
            has_month = any(p in ru_months or p in en_months for p in parts)
            has_year = any(_YEAR_RE.match(p) for p in parts)
            if has_month and has_year:
                kind = "month"
        periods.append({
            "col": idx + 1,
            "col_letter": col_letter,
            "label": label,
            "kind": kind,
        })
    return {
        "periods": periods,
        "_meta": {
            "sheet": sheet,
            "header_row": header_row,
            "count": len(periods),
            "kinds_seen": sorted({p["kind"] for p in periods}),
            "empty_reason": None if periods else "no_headers",
        },
    }


def diff_snapshot(before: dict, after: dict, max_examples: int = 10) -> dict:
    """Compare two snapshot_range() results. Returns {rows_added, rows_removed,
    cells_changed, examples}. Doesn't try to be a full diff library — just
    surfaces enough to verify "did the script write what we expected".
    """
    before_vals = before.get("values", []) or []
    after_vals = after.get("values", []) or []
    rows_added = max(0, len(after_vals) - len(before_vals))
    rows_removed = max(0, len(before_vals) - len(after_vals))
    cells_changed = 0
    examples = []
    for i in range(min(len(before_vals), len(after_vals))):
        b_row = before_vals[i] if i < len(before_vals) else []
        a_row = after_vals[i] if i < len(after_vals) else []
        for j in range(max(len(b_row), len(a_row))):
            b = b_row[j] if j < len(b_row) else ""
            a = a_row[j] if j < len(a_row) else ""
            if str(b) != str(a):
                cells_changed += 1
                if len(examples) < max_examples:
                    examples.append({"row": i + 1, "col": j + 1, "before": b, "after": a})
    # Sample new tail rows
    new_tail = []
    if rows_added:
        for i in range(len(before_vals), len(after_vals)):
            if len(new_tail) >= max_examples:
                break
            new_tail.append({"row": i + 1, "values": after_vals[i]})
    return {
        "before_rows": len(before_vals),
        "after_rows": len(after_vals),
        "rows_added": rows_added,
        "rows_removed": rows_removed,
        "cells_changed": cells_changed,
        "diff_examples": examples,
        "new_tail_rows": new_tail,
    }


# ============================================================================
# Phase 14 — bulk tools
# ============================================================================

import time as _time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

from src.tools import _bulk_payload as _bp

_MAX_BULK_WORKERS = 16  # ceiling; per-call max_workers also clamps


def _classify_bulk_exception(exc: Exception) -> str:
    """Lightweight error label for per-item bulk errors.

    Mirrors registry._classify_exception but kept here to avoid circular
    import (sheets → registry would cycle since registry imports sheets).
    """
    try:
        from googleapiclient.errors import HttpError as _HttpError
        if isinstance(exc, _HttpError):
            status = getattr(exc.resp, "status", 0) or 0
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 0
            if status == 404:
                return "not_found"
            if status in (401,):
                return "auth_scope"
            if status == 403:
                return "permission"
            if status == 429:
                return "rate_limit"
            if status >= 500:
                return "server"
            if status == 400:
                return "bad_input"
            return "unknown"
    except Exception:
        pass
    name = type(exc).__name__
    if name in {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError"}:
        return "network"
    if name in {"ValueError", "TypeError", "KeyError"}:
        return "bad_input"
    return "unknown"


def bulk_metric(
    spreadsheet_ids: list[str],
    cell: str,
    formatted: bool = False,
    max_workers: int = 10,
    dry_run: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read the SAME cell from N spreadsheets in parallel (Phase 14A).

    Use this for «one metric across many books with identical layout».
    Discover the layout ONCE via `metric_lookup(representative_id, metric)`,
    take its `cell` (e.g. "Год факт!B45"), then call `bulk_metric(rest, cell)`.
    Per-book cost: 1 Sheets API call. ThreadPoolExecutor parallelism (default
    10). Returns compacted payload — full per-book data spilled to disk and
    retrievable via `bulk_load_results(result_token)`.

    Args:
      spreadsheet_ids: list of spreadsheet IDs (≥1). Caller asserts they share layout.
      cell: full A1 ref. Accepts 'Sheet!A1' (preferred) or bare 'A1'.
      formatted: False → numbers as numbers; True → strings as displayed in UI.
      max_workers: parallel workers, clamped to [1, 16].
      dry_run: if True, return cost estimate without executing.
      account: which Google account.

    Returns:
      {stats:{n_ok, n_err, sum, mean, p50, p95, min, max}, outliers:{top, bottom},
       errors:[first 5], _meta:{result_token, n, duration_ms, op, cell, tool}}.
      Drill down to full per-book data via bulk_load_results(_meta.result_token).

    Quota: at N>50 prefer `sheets_cross_aggregate` (1 round-trip via Apps
    Script). bulk_metric burns N user-quota tokens against Sheets API.
    """
    if not isinstance(spreadsheet_ids, list) or not spreadsheet_ids:
        raise ValueError("spreadsheet_ids must be a non-empty list of spreadsheet IDs")
    if not isinstance(cell, str) or not cell.strip():
        raise ValueError(
            "cell is required (e.g. 'Год факт!B45' from metric_lookup output). "
            "bulk_metric has NO full-scan fallback — at N=500 a missed hint = 33-min quota wall."
        )

    n = len(spreadsheet_ids)
    workers = max(1, min(max_workers, _MAX_BULK_WORKERS, n))

    if dry_run:
        # Heuristics: empirical ~0.5s/cell-read; ThreadPool divides by workers.
        est_s = round(n * 0.5 / workers, 1)
        if n > 100:
            pressure = "high"
        elif n > 30:
            pressure = "medium"
        else:
            pressure = "ok"
        rec = None
        if n > 50:
            rec = "For N>50 use sheets_cross_aggregate — single Apps Script round-trip avoids quota pressure."
        return {
            "estimated_api_calls": n,
            "estimated_duration_s": est_s,
            "estimated_quota_pressure": pressure,
            "recommendation": rec,
            "_meta": {"dry_run": True, "n": n, "max_workers": workers, "cell": cell},
        }

    started = _time.perf_counter()
    items: list[dict] = []
    errors: list[dict] = []

    def _one(sid: str):
        try:
            r = read_range(sid, cell, formatted=formatted, account=account)
            vals = r.get("values") or []
            value = vals[0][0] if vals and vals[0] else None
            return ("ok", sid, value)
        except Exception as exc:
            return ("err", sid, exc)

    with _ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bulk_metric") as pool:
        futures = [pool.submit(_one, sid) for sid in spreadsheet_ids]
        for fut in futures:
            kind, sid, payload = fut.result()
            if kind == "ok":
                items.append({"id": sid, "value": payload})
            else:
                errors.append({
                    "id": sid,
                    "kind": _classify_bulk_exception(payload),
                    "msg": f"{type(payload).__name__}: {payload}"[:200],
                })

    return _bp.compact(
        items=items,
        op="sum",
        errors=errors,
        started_at=started,
        extra_meta={"cell": cell, "tool": "sheets_bulk_metric", "max_workers": workers},
    )


def bulk_read(
    refs: list[dict],
    formatted: bool = False,
    max_workers: int = 10,
    dry_run: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Parallel read of arbitrary {spreadsheet_id, range} pairs (Phase 14B).

    Generalizes bulk_metric: each ref names its own spreadsheet AND range.
    Use for «pull these specific 100 ranges across 20 books in one go».

    Args:
      refs: list of dicts, each {spreadsheet_id, range, formatted?}.
            `formatted` per-ref overrides the top-level `formatted`.
      formatted: default for refs that don't specify their own.
      max_workers: clamped [1, 16]. Default 10.
      dry_run: cost estimate without executing.
      account: which Google account.

    Returns compacted payload with one item per ref:
      {id: "<spreadsheet_id>:<range>", value: first_cell_if_single_else_None,
       dims: [row_count, col_count], range_read: echoed range}

    Full per-ref `values` grids spilled to disk; retrieve via
    `bulk_load_results(result_token)`.
    """
    if not isinstance(refs, list) or not refs:
        raise ValueError("refs must be a non-empty list of {spreadsheet_id, range} dicts")
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise ValueError(f"refs[{i}] must be a dict")
        if not ref.get("spreadsheet_id") or not ref.get("range"):
            raise ValueError(f"refs[{i}] must have non-empty 'spreadsheet_id' and 'range'")

    n = len(refs)
    workers = max(1, min(max_workers, _MAX_BULK_WORKERS, n))

    if dry_run:
        est_s = round(n * 0.5 / workers, 1)
        if n > 100:
            pressure = "high"
        elif n > 30:
            pressure = "medium"
        else:
            pressure = "ok"
        return {
            "estimated_api_calls": n,
            "estimated_duration_s": est_s,
            "estimated_quota_pressure": pressure,
            "recommendation": None,
            "_meta": {"dry_run": True, "n": n, "max_workers": workers},
        }

    started = _time.perf_counter()
    items: list[dict] = []
    full_items: list[dict] = []  # spilled to disk with full values grid
    errors: list[dict] = []

    def _one(ref: dict):
        sid = ref["spreadsheet_id"]
        rng = ref["range"]
        fmt = ref.get("formatted", formatted)
        try:
            r = read_range(sid, rng, formatted=fmt, account=account)
            values = r.get("values") or []
            rows = len(values)
            cols = max((len(row) for row in values), default=0)
            # If 1x1, surface the scalar so outliers/stats work.
            scalar = values[0][0] if rows == 1 and cols == 1 else None
            return ("ok", {
                "id": f"{sid}:{rng}",
                "value": scalar,
                "dims": [rows, cols],
                "range_read": r.get("_meta", {}).get("range_read", rng),
            }, values)
        except Exception as exc:
            return ("err", {"id": f"{sid}:{rng}", "exc": exc}, None)

    with _ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bulk_read") as pool:
        futures = [pool.submit(_one, ref) for ref in refs]
        for fut in futures:
            kind, item, full_values = fut.result()
            if kind == "ok":
                items.append(item)
                full_items.append({**item, "values": full_values})
            else:
                exc = item["exc"]
                errors.append({
                    "id": item["id"],
                    "kind": _classify_bulk_exception(exc),
                    "msg": f"{type(exc).__name__}: {exc}"[:200],
                })

    # Stash full grids alongside the compacted result — overrides compact's
    # default spill so drill-down returns the actual values, not the summary.
    token = _bp.make_token()
    _bp.write_result_file(token, {"items": full_items, "errors": errors, "op": "read"})
    _bp.cleanup_old()

    # Build compacted view manually (don't double-spill via compact()).
    values_for_stats = [it["value"] for it in items]
    stats = _bp.compute_stats(values_for_stats)
    stats["n_err"] = stats["n_err"] + len(errors)
    outliers = _bp.compute_outliers(items, op="sum")

    meta: dict = {
        "result_token": token,
        "n": len(items) + len(errors),
        "duration_ms": round((_time.perf_counter() - started) * 1000, 1),
        "op": "read",
        "tool": "sheets_bulk_read",
        "max_workers": workers,
    }
    if errors:
        meta["truncated"] = True
        meta["truncation_reason"] = f"{len(errors)} per-ref errors"

    return {
        "stats": stats,
        "outliers": outliers,
        "errors": errors[: _bp.MAX_ERRORS_LISTED],
        "_meta": meta,
    }


def cross_aggregate(
    spreadsheet_ids: list[str],
    sheet: str,
    cell: str,
    op: str = "sum",
    chunk_size: int = 100,
    max_concurrent: int = 5,
    max_iterations: int = 5,
    dry_run: bool = False,
    account: str = DEFAULT_ACCOUNT,  # accepted for tool symmetry; aggregator uses its own auth
) -> dict:
    """Server-side cross-book aggregation via persistent Apps Script (Phase 14C).

    Reads `sheet!cell` from each spreadsheet server-side. Splits N books into
    chunks of `chunk_size` (default 100) and runs them in parallel via
    ThreadPoolExecutor(`max_concurrent`). One Apps Script call per chunk =
    1 user-quota token per chunk (vs N tokens for direct Sheets reads).

    Why chunking: a single Apps Script call for 500 books takes ~3 min
    server-side, but Google's L7 load balancer drops the upstream connection
    at ~60s, causing RetryingHttpRequest to re-fire the script (turning a
    3-min run into a 30-min retry storm). Chunks of 100 books complete in
    ~60-90s each, fitting cleanly under the LB window.

    Args:
      spreadsheet_ids: list of spreadsheet IDs (≥1).
      sheet: tab name in each book (e.g. "Год факт"). Must match across books.
      cell: A1 ref in each book (e.g. "B45").
      op: "sum"|"avg"|"min"|"max"|"count"|"list".
      chunk_size: books per Apps Script invocation. Default 100.
      max_concurrent: parallel chunks. Default 5.
      max_iterations: per-chunk resumption cap. Default 5.
      dry_run: if True, return cost estimate without executing.

    Returns:
      {value, stats, outliers (empty — server-side), errors, _meta} —
      same shape as sheets_bulk_metric for agent ergonomics. `value` is
      the aggregate itself, merged across chunks.

    Setup: requires one-time `docs/PHASE_14_SETUP.md` deploy ceremony.
    Tool raises Phase14ConfigError on first call if not set up.
    """
    from src.tools import _apps_script_chunked, _phase14_config

    if dry_run:
        n = len(spreadsheet_ids) if isinstance(spreadsheet_ids, list) else 0
        n_chunks = max(1, (n + chunk_size - 1) // chunk_size)
        # Heuristic: ~50-90s per 100-book chunk; chunks parallel up to max_concurrent
        per_chunk_s = max(20.0, chunk_size * 0.7)
        parallel_waves = max(1, (n_chunks + max_concurrent - 1) // max_concurrent)
        est_s = round(per_chunk_s * parallel_waves, 1)
        return {
            "estimated_api_calls": n_chunks,  # 1 per chunk
            "estimated_duration_s": est_s,
            "estimated_quota_pressure": "ok",  # apps-script bucket is exempt
            "recommendation": None,
            "_meta": {
                "dry_run": True, "n": n, "tool": "sheets_cross_aggregate",
                "n_chunks": n_chunks, "max_concurrent": max_concurrent,
            },
        }

    script_id = _phase14_config.get_aggregator_script_id()
    return _apps_script_chunked.run_chunked_parallel(
        spreadsheet_ids=spreadsheet_ids,
        sheet=sheet,
        cell=cell,
        op=op,
        script_id=script_id,
        chunk_size=chunk_size,
        max_concurrent=max_concurrent,
        max_iterations=max_iterations,
        account=account,
    )


def cross_aggregate_status(
    token: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Peek at an in-flight cross_aggregate run by its resume token.

    Returns {status: "incomplete"|"not_found", processed_count, remaining_count, _meta}.
    Use when a previous cross_aggregate hit max_iterations and you want to
    decide whether to keep going or abort.
    """
    from src.tools import _apps_script_chunked, _phase14_config
    script_id = _phase14_config.get_aggregator_script_id()
    return _apps_script_chunked.fetch_status(token, script_id=script_id)


def bulk_load_results(
    result_token: str,
    offset: int = 0,
    limit: int = 150,
) -> dict:
    """Drill down to full per-item data from a previous bulk tool call.

    `result_token` is the value of `_meta.result_token` returned by
    sheets_bulk_metric / sheets_bulk_read. Paginated to fit MAX_TOOL_PAYLOAD:
    at default limit=150, returns ~10-12KB per page for typical Drive IDs.

    For 500-book results, agent makes 4 calls (offset=0,150,300,450) to get
    everything. `_meta.has_more` / `_meta.next_offset` tells you when to stop.

    Raises FileNotFoundError if the token expired (we keep at most 100
    most-recent bulk results).
    """
    full = _bp.load_result_file(result_token)
    items_all = full.get("items", []) or []
    errors_all = full.get("errors", []) or []

    total = len(items_all)
    end = min(offset + limit, total)
    page_items = items_all[offset:end]
    has_more = end < total

    return {
        "items": page_items,
        # Errors are always small (≤5 per real bulk call) — return all
        "errors": errors_all,
        "op": full.get("op"),
        "_meta": {
            "result_token": result_token,
            "loaded_from_disk": True,
            "offset": offset,
            "limit": limit,
            "page_size": len(page_items),
            "total": total,
            "has_more": has_more,
            "next_offset": end if has_more else None,
        },
    }
