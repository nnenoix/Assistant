from unittest.mock import MagicMock, patch

import pytest

from src.tools import drive


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(drive, "_service", return_value=svc):
        yield svc


def test_list_files_passes_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1", "name": "f"}]}
    result = drive.list_files(folder_id="ROOT", query=None)
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false",
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=50,
    )
    assert result == [{"id": "1", "name": "f"}]


def test_list_files_with_extra_query(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.list_files(folder_id="ROOT", query="name contains 'report'", page_size=10)
    fake_service.files().list.assert_called_with(
        q="'ROOT' in parents and trashed = false and (name contains 'report')",
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=10,
    )


def test_list_files_page_size_clamped(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.list_files(folder_id="ROOT", page_size=500)
    args = fake_service.files().list.call_args.kwargs
    assert args["pageSize"] == 200  # clamped at upper bound


def test_create_folder(fake_service):
    fake_service.files().create().execute.return_value = {"id": "NEW", "name": "X"}
    result = drive.create_folder(parent_id="P", name="X")
    fake_service.files().create.assert_called_with(
        body={"name": "X", "mimeType": "application/vnd.google-apps.folder", "parents": ["P"]},
        fields="id,name,mimeType,parents",
    )
    assert result == {"id": "NEW", "name": "X"}


def test_delete(fake_service):
    fake_service.files().delete().execute.return_value = None
    drive.delete(file_id="ABC")
    fake_service.files().delete.assert_called_with(fileId="ABC")


def test_rename(fake_service):
    fake_service.files().update().execute.return_value = {"id": "ABC", "name": "newname"}
    drive.rename(file_id="ABC", new_name="newname")
    fake_service.files().update.assert_called_with(
        fileId="ABC", body={"name": "newname"}, fields="id,name"
    )


def test_move(fake_service):
    fake_service.files().get().execute.return_value = {"parents": ["OLD"]}
    fake_service.files().update().execute.return_value = {"id": "ABC", "parents": ["NEW"]}
    drive.move(file_id="ABC", new_parent_id="NEW")
    fake_service.files().update.assert_called_with(
        fileId="ABC",
        addParents="NEW",
        removeParents="OLD",
        fields="id,parents",
    )


def test_search(fake_service):
    fake_service.files().list().execute.return_value = {"files": [{"id": "1"}]}
    drive.search("foo bar")
    fake_service.files().list.assert_called_with(
        q="name contains 'foo bar' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    )


def test_search_escapes_quotes(fake_service):
    fake_service.files().list().execute.return_value = {"files": []}
    drive.search("user's file")
    fake_service.files().list.assert_called_with(
        q="name contains 'user\\'s file' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    )
