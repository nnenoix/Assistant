"""Apps Script API tools (account-aware, no clasp dependency).

Lets the agent push code, create library versions, and manage deployments
under ANY configured account — including accounts the user controls but
that clasp is not logged in as. Cleanly separated from src/tools/apps_script.py
(which wraps the clasp CLI).

Requires OAuth scopes:
  - https://www.googleapis.com/auth/script.projects
  - https://www.googleapis.com/auth/script.deployments
(added to config.SCOPES). Existing tokens issued before these scopes were
added will fail with invalid_scope on first call — re-OAuth that account
via /accounts UI or `uv run python -m src.cli add <alias>`.
"""
from functools import lru_cache
from typing import Any

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("script", "v1", credentials=get_credentials(account), cache_discovery=False)


def get_content(script_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read all files in an Apps Script project. Returns
    {scriptId, files: [{name, type, source, lastModifyUser, ...}]}.
    `type` is one of: SERVER_JS, JSON (for appsscript.json), HTML.
    """
    return _service(account).projects().getContent(scriptId=script_id).execute()


def update_content(
    script_id: str,
    files: list[dict[str, Any]],
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Replace the FULL set of files in the project. `files` is a list of
    {name, type, source} entries — see get_content for shape. To edit just
    one file: get_content first, mutate the entry you want, send it back with
    all the others unchanged.
    """
    return _service(account).projects().updateContent(
        scriptId=script_id,
        body={"files": files},
    ).execute()


def create_version(
    script_id: str,
    description: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new version of the script project. Returns
    {scriptId, versionNumber, createTime, description}. This versionNumber
    is what you put in the consumer script's appsscript.json libraries
    section to pin the new code.
    """
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    return _service(account).projects().versions().create(
        scriptId=script_id, body=body,
    ).execute()


def list_versions(script_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List all versions of the project. Returns {versions: [{versionNumber,
    createTime, description}]}."""
    out = _service(account).projects().versions().list(scriptId=script_id).execute()
    return {"versions": out.get("versions", [])}


def get_project(script_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Metadata for the script project: title, parentId, owner, createTime."""
    return _service(account).projects().get(scriptId=script_id).execute()


def update_library_dependency(
    consumer_script_id: str,
    library_script_id: str,
    new_version: int,
    user_symbol: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """In `consumer_script_id`'s appsscript.json, find the library with
    `library_script_id` and set its version to `new_version`. If user_symbol
    is provided, it's used as the symbol/alias the library is exposed as.
    Other files are left untouched. Returns {updated_user_symbol, old_version,
    new_version}.
    """
    import json as _json

    content = get_content(consumer_script_id, account=account)
    files = list(content.get("files", []))

    manifest_idx = next(
        (i for i, f in enumerate(files) if f.get("name") == "appsscript" and f.get("type") == "JSON"),
        None,
    )
    if manifest_idx is None:
        raise ValueError(f"appsscript.json not found in {consumer_script_id}")

    manifest = _json.loads(files[manifest_idx]["source"])
    deps = manifest.setdefault("dependencies", {})
    libs = deps.setdefault("libraries", [])

    found = None
    old_version = None
    for lib in libs:
        if lib.get("libraryId") == library_script_id:
            old_version = lib.get("version")
            lib["version"] = new_version
            if user_symbol:
                lib["userSymbol"] = user_symbol
            found = lib
            break

    if found is None:
        new_lib = {"libraryId": library_script_id, "version": new_version, "developmentMode": False}
        if user_symbol:
            new_lib["userSymbol"] = user_symbol
        libs.append(new_lib)
        found = new_lib

    files[manifest_idx]["source"] = _json.dumps(manifest, ensure_ascii=False, indent=2)
    update_content(consumer_script_id, files, account=account)

    return {
        "library_id": library_script_id,
        "user_symbol": found.get("userSymbol"),
        "old_version": old_version,
        "new_version": new_version,
    }


def get_file(
    script_id: str,
    file_name: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Fetch ONE file from an Apps Script project and STAGE it locally to
    `.data/staging/<script_id>/<file_name>.gs`. Returns {staged_path,
    bytes, lines, type, preview_first_30_lines}. The agent should then
    read the full file via local_read_file (no truncation cap on local reads
    of staged files, since they're under our control), compose its fix, and
    push back with apps_script_api_edit_file.

    This is the canonical local-first read path — `get_content` returns the
    whole project (which is often huge) and gets truncated.
    """
    from src.config import DATA_DIR

    content = get_content(script_id, account=account)
    target = next((f for f in content.get("files", []) if f.get("name") == file_name), None)
    if target is None:
        names = [f.get("name") for f in content.get("files", [])]
        raise ValueError(f"file {file_name!r} not found in {script_id}; available: {names}")

    src = target.get("source", "")
    staging_dir = DATA_DIR / "staging" / script_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    # Map type to extension
    ext_map = {"SERVER_JS": ".gs", "JSON": ".json", "HTML": ".html"}
    ext = ext_map.get(target.get("type", "SERVER_JS"), ".gs")
    path = staging_dir / f"{file_name}{ext}"
    path.write_text(src, encoding="utf-8")

    lines = src.splitlines()
    preview = "\n".join(lines[:30])
    return {
        "staged_path": str(path),
        "bytes": len(src.encode("utf-8")),
        "lines": len(lines),
        "type": target.get("type"),
        "preview_first_30_lines": preview,
    }


def list_files(script_id: str, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """List file names + types + sizes for an Apps Script project — NO
    source content (so doesn't get truncated). Use this to see what's in
    the project, then fetch the specific file you need via get_file.
    """
    content = get_content(script_id, account=account)
    return [
        {
            "name": f.get("name"),
            "type": f.get("type"),
            "bytes": len((f.get("source") or "").encode("utf-8")),
            "lines": len((f.get("source") or "").splitlines()),
        }
        for f in content.get("files", [])
    ]


def find_bound_script(spreadsheet_id: str, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Find Apps Script projects bound to a specific spreadsheet. Bound
    scripts are NOT discoverable via Drive search — they don't appear in
    files.list with mimeType='script'. This helper brute-forces it:
    lists every script project visible to the account (via Drive's regular
    listing), then calls Apps Script API's projects.get on each to read
    parentId, filters to those whose parentId matches the spreadsheet.

    Returns [{script_id, title}] — typically 0 or 1 entries. Slow on
    accounts with many scripts (~1s per script).
    """
    from src.tools import drive as _drive

    candidates: list[dict] = []
    # Scripts the account OWNS (not shared)
    for f in _drive.list_files(folder_id="root", page_size=200, account=account):
        if f.get("mimeType") == "application/vnd.google-apps.script":
            candidates.append({"id": f["id"], "name": f.get("name")})
    # Also try a general scan via search of all scripts
    for f in _drive.search("", mime_type="application/vnd.google-apps.script", page_size=200, account=account):
        if f.get("mimeType") == "application/vnd.google-apps.script":
            if not any(c["id"] == f["id"] for c in candidates):
                candidates.append({"id": f["id"], "name": f.get("name")})

    matches: list[dict] = []
    for c in candidates:
        try:
            meta = get_project(c["id"], account=account)
            if meta.get("parentId") == spreadsheet_id:
                matches.append({"script_id": c["id"], "title": meta.get("title") or c.get("name")})
        except Exception:
            continue
    return matches


def edit_file(
    script_id: str,
    file_name: str,
    new_source: str,
    file_type: str = "SERVER_JS",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Convenience macro: read project, replace ONE file's source (or add it
    if missing), push back. Other files are preserved verbatim. Use this for
    'edit file X in script Y' without re-reading and re-sending the whole
    project manually.
    """
    content = get_content(script_id, account=account)
    files = list(content.get("files", []))

    idx = next((i for i, f in enumerate(files) if f.get("name") == file_name), None)
    if idx is None:
        files.append({"name": file_name, "type": file_type, "source": new_source})
        action = "created"
    else:
        files[idx] = {**files[idx], "source": new_source, "type": file_type}
        action = "replaced"

    update_content(script_id, files, account=account)
    return {"script_id": script_id, "file_name": file_name, "action": action, "bytes": len(new_source)}


def _find_function_span(source: str, function_name: str) -> tuple[int | None, int | None]:
    """Return (start, end) char offsets of the function in JS source, or
    (None, None) if not found. Handles nested braces, strings, line comments,
    block comments.
    """
    import re

    m = re.search(rf"^\s*function\s+{re.escape(function_name)}\s*\(", source, re.MULTILINE)
    if not m:
        return None, None
    start = m.start()
    # Find opening brace
    try:
        i = source.index("{", m.end())
    except ValueError:
        return None, None
    depth = 1
    j = i + 1
    in_mode: str | None = None  # "//"  "/*"  '"'  "'"  "`"
    while j < len(source):
        c = source[j]
        if in_mode == "//":
            if c == "\n":
                in_mode = None
        elif in_mode == "/*":
            if c == "*" and j + 1 < len(source) and source[j + 1] == "/":
                in_mode = None
                j += 1
        elif in_mode in ('"', "'", "`"):
            if c == "\\" and j + 1 < len(source):
                j += 1
            elif c == in_mode:
                in_mode = None
        else:
            if c == "/" and j + 1 < len(source):
                nxt = source[j + 1]
                if nxt == "/":
                    in_mode = "//"
                    j += 1
                elif nxt == "*":
                    in_mode = "/*"
                    j += 1
            elif c in ('"', "'", "`"):
                in_mode = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return start, j + 1
        j += 1
    return start, len(source)  # unbalanced


def replace_function(
    script_id: str,
    file_name: str,
    function_name: str,
    new_source: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Surgical edit: replace EXACTLY one function in a file, preserving
    everything else (other functions, comments, whitespace, file order).
    Walks JS braces to find the function span — safe with nested {} and
    strings. Use this when fixing a bug in a multi-function file rather
    than rewriting the whole file (which risks deleting unrelated code).
    """
    content = get_content(script_id, account=account)
    target = next((f for f in content.get("files", []) if f.get("name") == file_name), None)
    if target is None:
        raise ValueError(f"file {file_name!r} not in project {script_id}")

    src = target.get("source", "")
    start, end = _find_function_span(src, function_name)
    if start is None:
        raise ValueError(f"function {function_name!r} not found in {file_name!r}")

    # Ensure new_source has clean trailing newline structure
    if not new_source.endswith("\n"):
        new_source = new_source + "\n"
    new_full = src[:start] + new_source + src[end:]

    return edit_file(
        script_id=script_id,
        file_name=file_name,
        new_source=new_full,
        file_type=target.get("type", "SERVER_JS"),
        account=account,
    ) | {
        "replaced_function": function_name,
        "old_span": [start, end],
        "old_bytes": end - start,
        "new_bytes": len(new_source.encode("utf-8")),
    }
