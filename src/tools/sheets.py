import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build

from src.auth import get_credentials
from src.config import DATA_DIR


DEFAULT_ACCOUNT = "main"
BACKUPS_DIR = DATA_DIR / "sheets_backups"
BACKUPS_DIR.mkdir(exist_ok=True)


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("sheets", "v4", credentials=get_credentials(account), cache_discovery=False)


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


def read_range(spreadsheet_id: str, range: str, account: str = DEFAULT_ACCOUNT) -> list[list]:
    resp = _service(account).spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range
    ).execute()
    return resp.get("values", [])


def write_range(spreadsheet_id: str, range: str, values: list[list], account: str = DEFAULT_ACCOUNT) -> dict:
    snap = _snapshot(spreadsheet_id, range, account, "write_range")
    result = _service(account).spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    if snap:
        result["snapshot_id"] = snap
    return result


def append_rows(spreadsheet_id: str, range: str, values: list[list], account: str = DEFAULT_ACCOUNT) -> dict:
    # No snapshot for append — pure addition, undoable by deleting the new rows
    return _service(account).spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def clear_range(spreadsheet_id: str, range: str, account: str = DEFAULT_ACCOUNT) -> dict:
    snap = _snapshot(spreadsheet_id, range, account, "clear_range")
    result = _service(account).spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range, body={}
    ).execute()
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

        rng = f"'{sheet_name}'!A1:Z{1 + sample_n}"
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
        out["sheets"].append({
            "name": sheet_name,
            "grid": {"rows": rows_total, "cols": cols_total},
            "header": header,
            "sample_rows": sample,
            "read_error": err,
        })

    return out


def query(spreadsheet_id: str, source_range: str, sql: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Run a Google QUERY against a range in this spreadsheet — server-side
    aggregation that scales to millions of rows. Creates a TEMPORARY sheet
    inside the same file, writes the QUERY formula, reads the result, then
    deletes the temp sheet.

    `sql` uses Google's QUERY language: SELECT, WHERE, GROUP BY, ORDER BY,
    LIMIT. Columns are addressed positionally as Col1, Col2, ... or by letter
    A, B, ... depending on whether source_range has a header. We use header=1
    (treat first row of source_range as header).
    """
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
        # trim trailing empty rows / cols
        while values and not any(c not in (None, "") for c in (values[-1] or [])):
            values.pop()

        if values and values[0] and isinstance(values[0][0], str) and values[0][0].startswith("#"):
            return {"error": "; ".join(str(c) for c in values[0] if c), "rows": [], "row_count": 0}

        return {"rows": values, "row_count": len(values)}
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
    }


def find_in_spreadsheet(
    spreadsheet_id: str,
    query: str,
    case_sensitive: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> list[dict]:
    """Search for a substring across ALL sheets in one spreadsheet. Returns
    each match with sheet name, A1 cell, row/col indices, and the cell value.
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
        try:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=sheet_name
            ).execute()
            rows = resp.get("values", [])
        except Exception:
            continue

        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                hay = str(val)
                if not case_sensitive:
                    hay = hay.lower()
                if needle in hay:
                    matches.append({
                        "sheet": sheet_name,
                        "cell": f"{sheet_name}!{_col_to_a1(c_idx)}{r_idx + 1}",
                        "row": r_idx + 1,
                        "col": c_idx + 1,
                        "value": val,
                    })
    return matches


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
    rng = f"{sheet}!{column}1:{column}"
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
    values = read_range(spreadsheet_id, range, account=account)
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
