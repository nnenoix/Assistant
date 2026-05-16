from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("sheets", "v4", credentials=get_credentials(account), cache_discovery=False)


def read_range(spreadsheet_id: str, range: str, account: str = DEFAULT_ACCOUNT) -> list[list]:
    resp = _service(account).spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range
    ).execute()
    return resp.get("values", [])


def write_range(spreadsheet_id: str, range: str, values: list[list], account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def append_rows(spreadsheet_id: str, range: str, values: list[list], account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def clear_range(spreadsheet_id: str, range: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range, body={}
    ).execute()


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
