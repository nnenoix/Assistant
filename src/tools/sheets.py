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
