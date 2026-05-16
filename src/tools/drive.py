from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_ACCOUNT = "main"

# Short aliases the agent / user can use for mime_type in drive.search instead
# of the full Google mime string.
MIME_SHORTCUTS = {
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "doc": "application/vnd.google-apps.document",
    "document": "application/vnd.google-apps.document",
    "folder": "application/vnd.google-apps.folder",
    "presentation": "application/vnd.google-apps.presentation",
    "slides": "application/vnd.google-apps.presentation",
    "form": "application/vnd.google-apps.form",
    "script": "application/vnd.google-apps.script",
    "pdf": "application/pdf",
}


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


def search(name_contains: str, mime_type: str | None = None, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Search files by name across all files the account can see (own + shared).
    Optional `mime_type` filters by type — accepts both shortcuts ('spreadsheet',
    'doc', 'folder', 'pdf', etc.) and full Google mime strings.
    """
    safe = name_contains.replace("\\", "\\\\").replace("'", "\\'")
    q_parts = [f"name contains '{safe}'", "trashed = false"]
    if mime_type:
        mt = MIME_SHORTCUTS.get(mime_type.lower(), mime_type)
        q_parts.append(f"mimeType = '{mt}'")
    resp = _service(account).files().list(
        q=" and ".join(q_parts),
        fields="files(id,name,mimeType,modifiedTime,parents,owners(emailAddress))",
        pageSize=50,
    ).execute()
    return resp.get("files", [])


def search_everywhere(name_contains: str, mime_type: str | None = None) -> dict:
    """Run the same name search across EVERY configured account and aggregate.
    Returns {account_alias: [files]}. Useful when the user asks 'find X anywhere'
    without specifying which Google account it might live in.
    """
    from src import auth as _auth  # local import to avoid circular dependency at module load
    accounts = _auth.list_accounts() or [DEFAULT_ACCOUNT]
    return {acct: search(name_contains, mime_type=mime_type, account=acct) for acct in accounts}


def _analyze_names(files: list[dict], query: str) -> dict:
    """Tokenize file names and return structural signals — codes, years, doc
    types, frequent words. Pure data analysis, no API calls."""
    import re
    from collections import Counter

    if not files:
        return {
            "total_files": 0, "files": [],
            "recurring_codes_2_3_upper": {}, "year_tokens": {},
            "doc_type_candidates": {}, "common_other_words": {},
        }

    tokens: list[str] = []
    for f in files:
        # Split on whitespace and punctuation; keep words and digit runs
        tokens.extend(re.findall(r"[A-Za-zА-Яа-яёЁ]+|\d+", f["name"]))

    cnt = Counter(tokens)
    # Filter out tokens from the query itself so they don't dominate output
    query_words = {w.lower() for w in re.findall(r"\w+", query)}

    short_codes = {
        t: c for t, c in cnt.items()
        if 2 <= len(t) <= 3 and t.isalpha() and t.isupper() and c >= 2
    }
    year_tokens = {
        t: c for t, c in cnt.items()
        if t.isdigit() and len(t) == 4 and 2000 < int(t) < 2100
    }
    # Words ≥4 chars appearing in 2+ files — likely doc types, brand names, projects
    common_words = sorted(
        ((t, c) for t, c in cnt.items()
         if len(t) >= 4 and c >= 2 and t.lower() not in query_words and not t.isdigit()),
        key=lambda kv: kv[1], reverse=True,
    )

    # Heuristic: doc-type tokens are typical Russian/English financial doc names
    doc_type_words = {"ОПиУ", "ДДС", "Баланс", "Маржа", "Отчет", "Отчёт", "P&L", "PnL", "Финансы", "Активы"}
    doc_types = {t: c for t, c in common_words if t in doc_type_words}
    other_words = dict([(t, c) for t, c in common_words if t not in doc_type_words][:30])

    return {
        "total_files": len(files),
        "files": [
            {"name": f["name"], "id": f["id"], "modifiedTime": f.get("modifiedTime"),
             "owner": (f.get("owners") or [{}])[0].get("emailAddress")}
            for f in files
        ],
        "recurring_codes_2_3_upper": short_codes,
        "year_tokens": year_tokens,
        "doc_type_candidates": doc_types,
        "common_other_words": other_words,
        "hint": (
            "recurring_codes_2_3_upper and common_other_words are the categorical "
            "signals: brand codes, project names, departments. List EVERY entry "
            "from those buckets in your answer when the user asks 'what does X consist of'."
        ),
    }


def name_patterns(query: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Run drive_search and return STRUCTURAL ANALYSIS of the file names —
    recurring 2-3 letter uppercase codes (likely brand/project codes), year
    tokens, doc-type words, and frequent other words. Use this BEFORE reading
    any file when the user asks 'what brands/projects/clients does X have',
    'what does X consist of', 'из чего состоит X'. The categorical answer
    is in the file NAMES, this tool surfaces it without reading contents.
    """
    files = search(query, account=account)
    out = _analyze_names(files, query)
    out["query"] = query
    out["account"] = account
    return out


def name_patterns_everywhere(query: str) -> dict:
    """name_patterns aggregated across every configured account."""
    from src import auth as _auth
    accounts = _auth.list_accounts() or [DEFAULT_ACCOUNT]
    all_files: list[dict] = []
    seen_ids: set[str] = set()
    per_account: dict[str, int] = {}
    for acct in accounts:
        files = search(query, account=acct)
        per_account[acct] = len(files)
        for f in files:
            if f["id"] in seen_ids:
                continue
            seen_ids.add(f["id"])
            f = dict(f)
            f["_account"] = acct
            all_files.append(f)
    out = _analyze_names(all_files, query)
    out["query"] = query
    out["accounts_searched"] = accounts
    out["per_account_counts"] = per_account
    return out
