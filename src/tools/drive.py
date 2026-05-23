from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from src.auth import RetryingHttpRequest, get_credentials

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
    return build(
        "drive", "v3",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def _meta_from_list_resp(resp: dict, page_size_used: int) -> dict:
    """Build the `_meta` envelope for any files().list response. Drive's
    API doesn't return a total count — only a nextPageToken. If a token
    came back, there are more results we didn't fetch."""
    token = resp.get("nextPageToken")
    truncated = bool(token)
    files = resp.get("files", []) or []
    return {
        "returned_count": len(files),
        "truncated": truncated,
        "truncation_reason": (
            f"more results available — Drive returned a nextPageToken at page_size={page_size_used}. "
            "Increase page_size (max 200) or add a narrower filter."
            if truncated else None
        ),
        "empty_reason": None if files else "no_matches",
    }


def list_files(folder_id: str = "root", query: str | None = None,
               page_size: int = 50, response_format: str = "concise",
               account=DEFAULT_ACCOUNT) -> dict:
    """List files in a Drive folder on `account`. Returns {files, _meta}.

    `account` accepts: alias / "*" (all) / list of aliases. Multi-account
    runs aggregate and tag each file with `_account`.

    `response_format`:
      - "concise" (default): per-file `{id, name, mimeType, modifiedTime}`.
      - "detailed": adds `owners`, `size`, `parents`, `webViewLink`.
    """
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    accounts = _resolve_account_arg(account)
    if accounts is not None:
        return _aggregate_across_accounts(
            "list_files", accounts=accounts,
            folder_id=folder_id, query=query, page_size=page_size,
            response_format=response_format,
        )

    q = f"'{folder_id}' in parents and trashed = false"
    if query:
        q += f" and ({query})"
    ps = min(max(page_size, 1), 200)
    fields = "nextPageToken,files(id,name,mimeType,modifiedTime"
    if response_format == "detailed":
        fields += ",owners(emailAddress,displayName),size,parents,webViewLink"
    fields += ")"
    resp = _service(account).files().list(
        q=q, fields=fields, orderBy="modifiedTime desc", pageSize=ps,
    ).execute()
    return {"files": resp.get("files", []), "_meta": _meta_from_list_resp(resp, ps)}


def list_shared_with_me(page_size: int = 50, account=DEFAULT_ACCOUNT) -> dict:
    """List files shared with `account` ('Shared with me'). Returns {files, _meta}.

    `account` accepts: alias / "*" (all) / list of aliases.
    """
    accounts = _resolve_account_arg(account)
    if accounts is not None:
        return _aggregate_across_accounts(
            "list_shared_with_me", accounts=accounts, page_size=page_size,
        )

    ps = min(max(page_size, 1), 200)
    resp = _service(account).files().list(
        q="sharedWithMe = true and trashed = false",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,owners(emailAddress,displayName))",
        orderBy="modifiedTime desc",
        pageSize=ps,
    ).execute()
    return {"files": resp.get("files", []), "_meta": _meta_from_list_resp(resp, ps)}


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


def delete(file_id: str, dry_run: bool = False, account: str = DEFAULT_ACCOUNT) -> dict | None:
    """Permanently delete a Drive file. NOT reversible — the file bypasses
    trash. With `dry_run=True` returns a structured preview ({would_delete,
    name, mime_type, owner, size_bytes, ...}) WITHOUT performing the
    delete; nothing changes."""
    if dry_run:
        try:
            meta = _service(account).files().get(
                fileId=file_id,
                fields="id,name,mimeType,owners(emailAddress),size,modifiedTime,trashed",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            return {
                "dry_run": True,
                "executed": False,
                "plan": {
                    "would_call": "drive.files.delete",
                    "file_id": file_id,
                    "preview_error": str(e)[:200],
                    "note": "Could not fetch file metadata for preview; file may not exist or token may lack access.",
                },
                "_meta": {"native_preview": True},
            }
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "drive.files.delete",
                "file_id": meta.get("id"),
                "name": meta.get("name"),
                "mime_type": meta.get("mimeType"),
                "owner": (meta.get("owners") or [{}])[0].get("emailAddress"),
                "size_bytes": meta.get("size"),
                "modified_time": meta.get("modifiedTime"),
                "already_trashed": bool(meta.get("trashed")),
                "reversibility": (
                    "NOT REVERSIBLE — drive.delete bypasses trash; the file "
                    "is gone immediately. To preserve, move to a backup "
                    "folder via drive.update first."
                ),
            },
            "_meta": {"native_preview": True},
        }
    _service(account).files().delete(fileId=file_id).execute()
    return None


def copy(file_id: str, new_name: str | None = None, parent_id: str | None = None, account: str = DEFAULT_ACCOUNT) -> dict:
    body = {}
    if new_name:
        body["name"] = new_name
    if parent_id:
        body["parents"] = [parent_id]
    return _service(account).files().copy(
        fileId=file_id, body=body, fields="id,name,parents"
    ).execute()


def search(name_contains: str, mime_type: str | None = None, page_size: int = 50, account=DEFAULT_ACCOUNT) -> dict:
    """Search files by name across all files the account can see (own + shared).

    `account` accepts:
      - single alias ("main", "elena")
      - "*" — every configured account
      - list of aliases (["main", "elena"]) — explicit subset

    For "*" or list, results are aggregated; each file carries `_account`.
    Optional `mime_type` shortcuts: 'spreadsheet', 'doc', 'folder', 'pdf',
    'script', 'form'. Optional `page_size` (default 50, max 200).
    Returns {files, _meta:{truncated, ...}}.
    """
    accounts = _resolve_account_arg(account)
    if accounts is not None:
        return _aggregate_across_accounts(
            "search", name_contains, accounts=accounts,
            mime_type=mime_type, page_size=page_size,
        )

    safe = name_contains.replace("\\", "\\\\").replace("'", "\\'")
    q_parts = [f"name contains '{safe}'", "trashed = false"]
    if mime_type:
        mt = MIME_SHORTCUTS.get(mime_type.lower(), mime_type)
        q_parts.append(f"mimeType = '{mt}'")
    try:
        page_size_int = int(page_size)
    except (ValueError, TypeError):
        page_size_int = 50
    ps = min(max(page_size_int, 1), 200)
    resp = _service(account).files().list(
        q=" and ".join(q_parts),
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,owners(emailAddress))",
        pageSize=ps,
    ).execute()
    return {"files": resp.get("files", []), "_meta": _meta_from_list_resp(resp, ps)}


def _resolve_account_arg(account) -> list[str] | None:
    """Normalize the `account` parameter for multi-account aggregation.

    Returns:
      - None when caller wants single-account path (string alias)
      - list[str] of accounts when "*" or explicit list provided

    Empty lists are treated as None (caller's single default account).
    """
    if isinstance(account, list):
        accts = [a for a in account if a]
        return accts if accts else None
    if account == "*":
        from src import auth as _auth
        return _auth.list_accounts() or [DEFAULT_ACCOUNT]
    return None


def _aggregate_across_accounts(fn_name: str, *args, accounts: list[str] | None = None, **kwargs) -> dict:
    """Run a drive function across `accounts` and merge.

    `fn_name` ∈ {"search", "list_files", "list_shared_with_me", "name_patterns"}.
    If `accounts` is None, falls back to every configured account
    (preserves the legacy "*" semantics).

    Returns {files, _meta:{accounts_searched, per_account_counts,
    truncated, truncation_reason, empty_reason}}.
    """
    if accounts is None:
        from src import auth as _auth
        accounts = _auth.list_accounts() or [DEFAULT_ACCOUNT]
    # Dedupe while preserving order (caller might pass duplicates)
    seen_accts: set[str] = set()
    accounts = [a for a in accounts if not (a in seen_accts or seen_accts.add(a))]
    fn = globals()[fn_name]
    all_files: list[dict] = []
    seen_ids: set[str] = set()
    per_account_counts: dict[str, int] = {}
    truncated_accounts: list[str] = []
    errors: list[dict] = []
    for acct in accounts:
        try:
            resp = fn(*args, account=acct, **kwargs)
        except Exception as e:
            per_account_counts[acct] = -1  # signals error for this account
            errors.append({
                "account": acct,
                "kind": type(e).__name__,
                "message": str(e)[:300],
            })
            continue
        files = resp.get("files", []) if isinstance(resp, dict) else []
        per_account_counts[acct] = len(files)
        if resp.get("_meta", {}).get("truncated"):
            truncated_accounts.append(acct)
        for f in files:
            fid = f.get("id")
            if not fid or fid in seen_ids:
                continue
            seen_ids.add(fid)
            f = dict(f)
            f["_account"] = acct
            all_files.append(f)
    meta = {
        "returned_count": len(all_files),
        "accounts_searched": accounts,
        "per_account_counts": per_account_counts,
        "truncated": bool(truncated_accounts),
        "truncation_reason": (
            f"results clipped on: {', '.join(truncated_accounts)}"
            if truncated_accounts else None
        ),
        "empty_reason": None if all_files else "no_matches",
    }
    if errors:
        # Surface per-account failures so the agent sees partial results aren't
        # the full picture (rule 23 — don't silently swallow). First 5 keeps
        # payload small; full list isn't needed for triage.
        meta["errors"] = errors[:5]
        meta["error_count"] = len(errors)
        meta["warning"] = (
            f"{len(errors)}/{len(accounts)} accounts failed; results are partial"
        )
    return {"files": all_files, "_meta": meta}


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


def name_patterns(query: str, account=DEFAULT_ACCOUNT) -> dict:
    """Run drive_search and return STRUCTURAL ANALYSIS of the file names —
    recurring 2-3 letter uppercase codes (likely brand/project codes), year
    tokens, doc-type words, and frequent other words. Use this BEFORE reading
    any file when the user asks 'what brands/projects/clients does X have',
    'what does X consist of', 'из чего состоит X'. The categorical answer
    is in the file NAMES, this tool surfaces it without reading contents.

    `account` accepts: alias / "*" (all) / list of aliases. Multi-account
    runs dedupe files by id and tag each with `_account`.
    """
    accounts = _resolve_account_arg(account)
    if accounts is not None:
        all_files: list[dict] = []
        seen_ids: set[str] = set()
        per_account: dict[str, int] = {}
        truncated_accounts: list[str] = []
        for acct in accounts:
            try:
                resp = search(query, account=acct)
            except Exception:
                per_account[acct] = -1
                continue
            files = resp.get("files", [])
            if resp.get("_meta", {}).get("truncated"):
                truncated_accounts.append(acct)
            per_account[acct] = len(files)
            for f in files:
                if not f.get("id") or f["id"] in seen_ids:
                    continue
                seen_ids.add(f["id"])
                f = dict(f)
                f["_account"] = acct
                all_files.append(f)
        out = _analyze_names(all_files, query)
        out["query"] = query
        out["accounts_searched"] = accounts
        out["per_account_counts"] = per_account
        out["_meta"] = {
            "truncated": bool(truncated_accounts),
            "truncated_accounts": truncated_accounts,
            "truncation_reason": (
                f"results clipped on: {', '.join(truncated_accounts)}"
                if truncated_accounts else None
            ),
        }
        return out

    resp = search(query, account=account)
    files = resp.get("files", [])
    out = _analyze_names(files, query)
    out["query"] = query
    out["account"] = account
    # Propagate truncation so callers don't claim completeness when Drive clipped.
    out["_meta"] = resp.get("_meta", {})
    return out


# -------- Phase 4: permissions / sharing --------

_VALID_ROLES = {"reader", "commenter", "writer", "owner", "organizer", "fileOrganizer"}


def list_permissions(file_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List who has access to `file_id`. Returns {permissions, _meta}.

    Each permission has id, role, type ('user'|'group'|'domain'|'anyone'),
    emailAddress (when type=user), displayName, deleted, pendingOwner.
    """
    resp = _service(account).permissions().list(
        fileId=file_id,
        fields="permissions(id,type,role,emailAddress,displayName,deleted,pendingOwner)",
        supportsAllDrives=True,
    ).execute()
    perms = resp.get("permissions", []) or []
    return {
        "permissions": perms,
        "_meta": {
            "count": len(perms),
            "empty_reason": None if perms else "no_permissions",
        },
    }


def share(
    file_id: str,
    email: str,
    role: str = "reader",
    notify: bool = True,
    message: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Grant `email` access to `file_id` at `role` level.

    Roles: reader (view-only), commenter, writer (edit), owner (transfers
    ownership — see `transfer_ownership` for full flow). `notify=True`
    sends Google's standard email; set False to share silently.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"unknown role {role!r}; allowed: {sorted(_VALID_ROLES)}")
    body = {"type": "user", "role": role, "emailAddress": email}
    kwargs = {
        "fileId": file_id,
        "body": body,
        "fields": "id,emailAddress,role,displayName",
        "sendNotificationEmail": notify,
        "supportsAllDrives": True,
    }
    if notify and message:
        kwargs["emailMessage"] = message
    resp = _service(account).permissions().create(**kwargs).execute()
    return {"ok": True, "permission_id": resp.get("id"), "email": resp.get("emailAddress"), "role": resp.get("role")}


def revoke_permission(file_id: str, permission_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Remove a permission by its id (get it from list_permissions)."""
    _service(account).permissions().delete(
        fileId=file_id,
        permissionId=permission_id,
        supportsAllDrives=True,
    ).execute()
    return {"ok": True, "permission_id": permission_id}


def transfer_ownership(file_id: str, new_owner_email: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Transfer ownership to `new_owner_email`.

    Google's API requires `transferOwnership=True` AND the new owner must
    be in the same Workspace organization (personal Gmail → personal Gmail
    is allowed; personal → Workspace requires the receiver to accept).
    For consumer Gmail accounts, the recipient gets a pending-ownership
    notification they must accept.
    """
    resp = _service(account).permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "owner", "emailAddress": new_owner_email},
        transferOwnership=True,
        sendNotificationEmail=True,
        fields="id,role,pendingOwner,emailAddress",
        supportsAllDrives=True,
    ).execute()
    return {
        "ok": True,
        "permission_id": resp.get("id"),
        "new_owner": resp.get("emailAddress"),
        "pending_owner": resp.get("pendingOwner", False),
    }


# -------- Phase 4: revisions --------

def list_revisions(file_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List version history of a Drive file. Returns {revisions, _meta}.

    Works on any file type. For native Google formats (Sheets/Docs/Slides)
    each revision is auto-saved by Google; for uploaded binaries (PDF,
    images) each upload-update is a revision.
    """
    resp = _service(account).revisions().list(
        fileId=file_id,
        fields="revisions(id,modifiedTime,lastModifyingUser(displayName,emailAddress),size,mimeType,keepForever)",
    ).execute()
    revisions = resp.get("revisions", []) or []
    return {
        "revisions": revisions,
        "_meta": {
            "count": len(revisions),
            "empty_reason": None if revisions else "no_revisions",
        },
    }


def download_revision(
    file_id: str,
    revision_id: str,
    dest_path: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Download a specific revision to a local file.

    Note: Drive API supports binary revision download (PDFs, images,
    .xlsx, etc.) but NOT native Google format revisions — calling this
    on a Sheets/Docs/Slides revision will raise. For native formats,
    use revision metadata + Drive UI's version history instead.
    """
    import io
    from googleapiclient.http import MediaIoBaseDownload

    request = _service(account).revisions().get_media(fileId=file_id, revisionId=revision_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return {"ok": True, "dest_path": dest_path, "revision_id": revision_id}


# -------- Phase 4: comments --------

def add_comment(
    file_id: str,
    content: str,
    anchor: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Add a top-level Drive comment to a file. Works on Docs/Sheets/Slides/PDFs.

    For anchored comments (pointing to a specific location in Docs/Sheets),
    pass `anchor` as the JSON anchor string Drive expects. For a free-form
    file-level comment, leave anchor None.
    """
    body = {"content": content}
    if anchor:
        body["anchor"] = anchor
    resp = _service(account).comments().create(
        fileId=file_id,
        body=body,
        fields="id,content,createdTime,author(displayName,emailAddress),anchor",
    ).execute()
    return {"ok": True, "comment_id": resp.get("id"), "content": resp.get("content"), "anchor": resp.get("anchor")}


def list_comments(
    file_id: str,
    include_resolved: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """List comments on a file. Returns {comments, _meta}. By default skips
    resolved comments — pass `include_resolved=True` to see them too."""
    resp = _service(account).comments().list(
        fileId=file_id,
        includeDeleted=False,
        fields="comments(id,content,createdTime,modifiedTime,author(displayName,emailAddress),resolved,anchor,deleted,replies(id,content,createdTime,author(displayName,emailAddress)))",
    ).execute()
    comments = resp.get("comments", []) or []
    if not include_resolved:
        comments = [c for c in comments if not c.get("resolved")]
    return {
        "comments": comments,
        "_meta": {
            "count": len(comments),
            "include_resolved": include_resolved,
            "empty_reason": None if comments else "no_comments",
        },
    }


def resolve_comment(file_id: str, comment_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Mark a comment as resolved (Drive's 'Done' button).

    Implementation note: Drive's `comments.update` doesn't actually flip the
    `resolved` field for Sheets — it silently succeeds but the comment
    stays open. The correct mechanism is to create a REPLY with
    `action="resolve"`, which is what Drive's UI does behind the scenes.
    """
    resp = _service(account).replies().create(
        fileId=file_id,
        commentId=comment_id,
        body={"action": "resolve"},
        fields="id,action",
    ).execute()
    return {
        "ok": True,
        "comment_id": comment_id,
        "reply_id": resp.get("id"),
        "resolved": True,
    }


# -------- Phase 4: trash management --------

def list_trash(page_size: int = 50, account: str = DEFAULT_ACCOUNT) -> dict:
    """List files in the trash. Returns {files, _meta:{truncated, ...}}."""
    ps = min(max(page_size, 1), 200)
    resp = _service(account).files().list(
        q="trashed = true",
        fields="nextPageToken,files(id,name,mimeType,modifiedTime,trashedTime,owners(emailAddress))",
        orderBy="recency desc",
        pageSize=ps,
    ).execute()
    return {"files": resp.get("files", []), "_meta": _meta_from_list_resp(resp, ps)}


def restore_from_trash(file_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Restore a trashed file (sets trashed=false)."""
    resp = _service(account).files().update(
        fileId=file_id,
        body={"trashed": False},
        fields="id,name,trashed",
    ).execute()
    return {"ok": True, "file_id": resp.get("id"), "name": resp.get("name"), "trashed": resp.get("trashed")}


def empty_trash(account: str = DEFAULT_ACCOUNT) -> dict:
    """Permanently delete EVERYTHING in the trash. Irreversible — use with care."""
    _service(account).files().emptyTrash().execute()
    return {"ok": True, "warning": "trash permanently emptied"}


def resolve_link(url: str, accounts: list[str] | None = None) -> dict:
    """Resolve a Drive / Docs / Sheets / Slides share-link by probing every
    OAuth-registered account until one of them can see it.

    This is the answer to "user pasted a link — under which account does it
    work?". The agent calls this BEFORE running drive_*/sheets_* tools
    when a URL appears in the conversation, so it picks the right
    `account=` alias automatically.

    Returns:
        when at least one account has access:
        {
            ok: True,
            parsed: {kind, id},          # what the URL pointed at
            accessible_via: ["egor", "elena"],  # aliases that see it
            not_seen_by: ["work"],       # aliases that got 404 / 403
            recommended_account: "egor", # the first one that works
            metadata: {name, mimeType, webViewLink, …}  # from the recommended acct
            _meta: {probed: N, took_ms}
        }

        when nobody has access:
        {
            ok: False,
            parsed: {kind, id},
            accessible_via: [],
            not_seen_by: ["egor", "work"],
            error_kind: "permission",  # or "not_found" / "auth_scope"
            suggestion: "add_account",  # call POST /api/accounts/add_auto
            hint: "Sign in with the Google account that has access to this link.",
            _meta: {probed: N, took_ms}
        }

    Use:
        out = drive.resolve_link("https://drive.google.com/drive/folders/...")
        if out["ok"]:
            drive.list_files(folder_id=out["parsed"]["id"],
                             account=out["recommended_account"])
    """
    from src.tools.browser import _parse_drive_url  # already battle-tested
    from src import auth
    from src.tools._errors import _classify_exception
    import time

    t0 = time.perf_counter()
    parsed = _parse_drive_url(url)
    if parsed["kind"] == "unknown" or not parsed["id"]:
        return {
            "ok": False,
            "parsed": parsed,
            "error_kind": "bad_input",
            "error": "URL doesn't look like a Drive/Docs/Sheets/Slides link",
            "_meta": {"probed": 0, "took_ms": 0},
        }

    candidates = accounts if accounts is not None else auth.list_accounts()
    if not candidates:
        return {
            "ok": False,
            "parsed": parsed,
            "accessible_via": [],
            "not_seen_by": [],
            "error_kind": "auth_scope",
            "suggestion": "add_account",
            "hint": (
                "No Google accounts are registered yet. Sign in via "
                "POST /api/accounts/add_auto to add the first one."
            ),
            "_meta": {"probed": 0, "took_ms": 0},
        }

    # Probe accounts in PARALLEL — serial loop made the agent wait
    # 4×~150ms (one full RTT per account) before knowing which alias
    # owns access. ThreadPoolExecutor cuts this to ~150ms total.
    from concurrent.futures import ThreadPoolExecutor

    def _probe(alias: str) -> tuple[str, dict | None, dict | None]:
        try:
            return alias, get_metadata(parsed["id"], account=alias), None
        except Exception as e:
            kind, status = _classify_exception(e)
            return alias, None, {"alias": alias, "error_kind": kind,
                                  "http_status": status}

    accessible: list[str] = []
    blocked: list[dict] = []
    per_alias_meta: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(candidates), 8),
                            thread_name_prefix="drive-resolve") as pool:
        for alias, meta, err in pool.map(_probe, candidates):
            if meta is not None:
                accessible.append(alias)
                per_alias_meta[alias] = meta
            elif err is not None:
                blocked.append(err)

    # Pick the first accessible IN ORIGINAL `candidates` ORDER (not the
    # order futures happened to complete). Makes the "recommended" alias
    # deterministic across re-runs.
    found_account: str | None = next(
        (a for a in candidates if a in per_alias_meta), None
    )
    found_meta: dict | None = per_alias_meta.get(found_account) if found_account else None
    # `accessible` is built in completion order — re-sort to match candidates
    # order too, so the UI shows aliases in a stable sequence.
    accessible = [a for a in candidates if a in per_alias_meta]

    took_ms = round((time.perf_counter() - t0) * 1000, 1)
    if accessible:
        return {
            "ok": True,
            "parsed": parsed,
            "accessible_via": accessible,
            "not_seen_by": [b["alias"] for b in blocked],
            "recommended_account": found_account,
            "metadata": found_meta,
            "_meta": {"probed": len(candidates), "took_ms": took_ms},
        }

    # Nobody saw it — figure out the dominant failure kind so we can
    # surface the right hint.
    kinds = [b["error_kind"] for b in blocked]
    dominant = "not_found"
    if "permission" in kinds:
        dominant = "permission"
    elif "auth_scope" in kinds:
        dominant = "auth_scope"
    hints = {
        "not_found": "None of the registered accounts can see this link. "
                     "Either the link is wrong, OR it's shared with an account "
                     "you haven't added yet — use POST /api/accounts/add_auto "
                     "to sign in with the right Google account.",
        "permission": "The link exists, but all your registered accounts are "
                      "denied access. Add the account that has access via "
                      "POST /api/accounts/add_auto, or ask the owner to share "
                      "with one of: " + ", ".join(candidates),
        "auth_scope": "One or more accounts are missing the Drive scope. "
                      "Re-run OAuth for them via auth_add_account_incremental.",
    }
    return {
        "ok": False,
        "parsed": parsed,
        "accessible_via": [],
        "not_seen_by": [b["alias"] for b in blocked],
        "blocked_detail": blocked,
        "error_kind": dominant,
        "suggestion": "add_account",
        "hint": hints[dominant],
        "_meta": {"probed": len(candidates), "took_ms": took_ms},
    }
