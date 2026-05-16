from unittest.mock import MagicMock, patch

import pytest

from src.tools import sheets


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(sheets, "_service", return_value=svc):
        yield svc


def test_read_range(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {"values": [["a", "b"], ["c", "d"]]}
    result = sheets.read_range(spreadsheet_id="SID", range="Sheet1!A1:B2")
    fake_service.spreadsheets().values().get.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A1:B2"
    )
    assert result == [["a", "b"], ["c", "d"]]


def test_read_range_empty_returns_empty_list(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {}
    assert sheets.read_range("SID", "Sheet1!A1") == []


def test_write_range(fake_service):
    fake_service.spreadsheets().values().update().execute.return_value = {"updatedCells": 4}
    result = sheets.write_range("SID", "Sheet1!A1:B2", [[1, 2], [3, 4]])
    fake_service.spreadsheets().values().update.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1:B2",
        valueInputOption="USER_ENTERED",
        body={"values": [[1, 2], [3, 4]]},
    )
    assert result == {"updatedCells": 4}


def test_append_rows(fake_service):
    fake_service.spreadsheets().values().append().execute.return_value = {"updates": {"updatedRows": 2}}
    sheets.append_rows("SID", "Sheet1!A1", [["x"], ["y"]])
    fake_service.spreadsheets().values().append.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [["x"], ["y"]]},
    )


def test_clear_range(fake_service):
    fake_service.spreadsheets().values().clear().execute.return_value = {}
    sheets.clear_range("SID", "Sheet1!A:Z")
    fake_service.spreadsheets().values().clear.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A:Z", body={}
    )


def test_create_spreadsheet(fake_service):
    fake_service.spreadsheets().create().execute.return_value = {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}
    result = sheets.create_spreadsheet(title="My Report")
    fake_service.spreadsheets().create.assert_called_with(
        body={"properties": {"title": "My Report"}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    )
    assert result == {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}


def test_add_sheet(fake_service):
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99, "title": "T"}}}]
    }
    result = sheets.add_sheet("SID", "T")
    fake_service.spreadsheets().batchUpdate.assert_called_with(
        spreadsheetId="SID",
        body={"requests": [{"addSheet": {"properties": {"title": "T"}}}]},
    )
    assert result == {"sheetId": 99, "title": "T"}
