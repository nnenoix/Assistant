from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"


@lru_cache(maxsize=1)
def _service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def list_files(folder_id: str = "root", query: str | None = None) -> list[dict]:
    q = f"'{folder_id}' in parents and trashed = false"
    if query:
        q += f" and ({query})"
    resp = _service().files().list(
        q=q,
        fields="files(id,name,mimeType,modifiedTime,size,parents)",
        pageSize=200,
    ).execute()
    return resp.get("files", [])


def get_metadata(file_id: str) -> dict:
    return _service().files().get(
        fileId=file_id,
        fields="id,name,mimeType,modifiedTime,size,parents,webViewLink",
    ).execute()


def create_folder(parent_id: str, name: str) -> dict:
    return _service().files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id,name,mimeType,parents",
    ).execute()


def upload(local_path: str, parent_id: str, name: str | None = None, mime_type: str | None = None) -> dict:
    p = Path(local_path)
    if not p.exists():
        raise FileNotFoundError(local_path)
    media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
    body = {"name": name or p.name, "parents": [parent_id]}
    return _service().files().create(
        body=body, media_body=media, fields="id,name,mimeType,parents,webViewLink"
    ).execute()


def download(file_id: str, dest_path: str) -> str:
    import io
    request = _service().files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()
    return dest_path


def update_content(file_id: str, local_path: str, mime_type: str | None = None) -> dict:
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    return _service().files().update(
        fileId=file_id, media_body=media, fields="id,name,modifiedTime"
    ).execute()


def rename(file_id: str, new_name: str) -> dict:
    return _service().files().update(
        fileId=file_id, body={"name": new_name}, fields="id,name"
    ).execute()


def move(file_id: str, new_parent_id: str) -> dict:
    meta = _service().files().get(fileId=file_id, fields="parents").execute()
    old_parents = ",".join(meta.get("parents", []))
    return _service().files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id,parents",
    ).execute()


def delete(file_id: str) -> None:
    _service().files().delete(fileId=file_id).execute()


def copy(file_id: str, new_name: str | None = None, parent_id: str | None = None) -> dict:
    body = {}
    if new_name:
        body["name"] = new_name
    if parent_id:
        body["parents"] = [parent_id]
    return _service().files().copy(
        fileId=file_id, body=body, fields="id,name,parents"
    ).execute()


def search(name_contains: str) -> list[dict]:
    safe = name_contains.replace("\\", "\\\\").replace("'", "\\'")
    resp = _service().files().list(
        q=f"name contains '{safe}' and trashed = false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=50,
    ).execute()
    return resp.get("files", [])
