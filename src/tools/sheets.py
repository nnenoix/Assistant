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
