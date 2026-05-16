from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("drive", "v3", credentials=get_credentials(account), cache_discovery=False)


def list_files(folder_id: str = "root", query: str | None = None, page_size: int = 50, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """List files in a Drive folder on `account`. Returns slim metadata."""
    q = f"'{folder_id}' in parents and trashed = false"
    if query:
        q += f" and ({query})"
    resp = _service(account).files().list(
        q=q,
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=min(max(page_size, 1), 200),
    ).execute()
    return resp.get("files", [])


def list_shared_with_me(page_size: int = 50, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """List files shared with `account` ('Shared with me')."""
    resp = _service(account).files().list(
        q="sharedWithMe = true and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,owners(emailAddress,displayName))",
        orderBy="modifiedTime desc",
        pageSize=min(max(page_size, 1), 200),
    ).execute()
    return resp.get("files", [])


def get_metadata(file_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).files().get(
        fileId=file_id,
        fields="id,name,mimeType,modifiedTime,size,parents,webViewLink",
    ).execute()


def create_folder(parent_id: str, name: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id,name,mimeType,parents",
    ).execute()


def upload(local_path: str, parent_id: str, name: str | None = None, mime_type: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    p = Path(local_path)
    if not p.exists():
        raise FileNotFoundError(local_path)
    media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
    body = {"name": name or p.name, "parents": [parent_id]}
    return _service(account).files().create(
        body=body, media_body=media, fields="id,name,mimeType,parents,webViewLink"
    ).execute()


def download(file_id: str, dest_path: str, account: str = DEFAULT_ACCOUNT) -> str:
    import io
    request = _service(account).files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()
    return dest_path


def update_content(file_id: str, local_path: str, mime_type: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    return _service(account).files().update(
        fileId=file_id, media_body=media, fields="id,name,modifiedTime"
    ).execute()


def rename(file_id: str, new_name: str, account: str = DEFAULT_ACCOUNT) -> dict:
    return _service(account).files().update(
        fileId=file_id, body={"name": new_name}, fields="id,name"
    ).execute()


def move(file_id: str, new_parent_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    meta = _service(account).files().get(fileId=file_id, fields="parents").execute()
    old_parents = ",".join(meta.get("parents", []))
    return _service(account).files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id,parents",
    ).execute()


def delete(file_id: str, account: str = DEFAULT_ACCOUNT) -> None:
    _service(account).files().delete(fileId=file_id).execute()


def copy(file_id: str, new_name: str | None = None, parent_id: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    body = {}
    if new_name:
        body["name"] = new_name
    if parent_id:
        body["parents"] = [parent_id]
    return _service(account).files().copy(
        fileId=file_id, body=body, fields="id,name,parents"
    ).execute()


def search(name_contains: str, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    safe = name_contains.replace("\\", "\\\\").replace("'", "\\'")
    resp = _service(account).files().list(
        q=f"name contains '{safe}' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    ).execute()
    return resp.get("files", [])
