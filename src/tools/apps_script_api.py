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
