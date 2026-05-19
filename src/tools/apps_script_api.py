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
import json
from functools import lru_cache
from typing import Any

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("script", "v1", credentials=get_credentials(account), cache_discovery=False)


def create_project(
    title: str,
    parent_id: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new standalone Apps Script project. Returns {scriptId, title,
    createTime, ...}. If `parent_id` is a Drive folder/spreadsheet ID, the
    script is created as bound to it. Owner is `account`.

    Use this for ad-hoc test scripts when you need to run code with a specific
    library dependency or just call something via the Apps Script API. After
    create, push files with update_content / edit_file.
    """
    body: dict[str, Any] = {"title": title}
    if parent_id is not None:
        body["parentId"] = parent_id
    return _service(account).projects().create(body=body).execute()


def run_function(
    script_id: str,
    function_name: str,
    params: list | None = None,
    dev_mode: bool = True,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Run an Apps Script function via the Apps Script API and return its
    result. The script must have `executionApi.access` set in appsscript.json
    (e.g. {"executionApi": {"access": "MYSELF"}}) and the calling account must
    be the owner/editor.

    `dev_mode=True` runs HEAD code (latest source) without a deployment —
    convenient for testing. `dev_mode=False` runs the API-exec deployment's
    pinned version (create one via create_deployment first).

    Returns a normalized dict:
      - {ok: True, result: <return value>} on success
      - {ok: False, error_type, error_message, stack: [{function, line}, ...]} on script error
    Both forms include `raw` with the full API response for debugging.

    Requires scope: https://www.googleapis.com/auth/script.scriptapp
    """
    svc = _service(account)
    body: dict[str, Any] = {"function": function_name, "devMode": bool(dev_mode)}
    if params is not None:
        body["parameters"] = params
    resp = svc.scripts().run(scriptId=script_id, body=body).execute()

    if "error" in resp:
        err = resp["error"]
        details = err.get("details") or [{}]
        d0 = details[0] if details else {}
        return {
            "ok": False,
            "error_type": d0.get("errorType") or err.get("status"),
            "error_message": d0.get("errorMessage") or err.get("message"),
            "stack": d0.get("scriptStackTraceElements", []),
            "raw": resp,
        }
    return {
        "ok": True,
        "result": (resp.get("response") or {}).get("result"),
        "raw": resp,
    }


def run_ad_hoc(
    code: str,
    function_name: str = "main",
    params: list | None = None,
    library_id: str | None = None,
    library_version: int | None = None,
    library_symbol: str = "Mylib",
    keep_project: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """ONE-SHOT: create a temp Apps Script project, push code, run it, delete.
    All under `account` — no clasp, no OAuth mixing.

    The project's manifest is auto-built with executionApi.access=MYSELF so the
    function is runnable. If `library_id` + `library_version` are given, the
    library is wired up at `library_symbol` (default 'Mylib') — useful for
    testing a library's exported functions.

    Returns the same shape as run_function: {ok, result | error_type+...}
    plus {script_id, script_url}. If keep_project=True, the script stays
    in the account's Drive so you can inspect/run it again.

    Use this for: testing a library function with real arguments, ad-hoc
    poke-the-system scripts, "what does this WB token return" checks.
    """
    import json as _json

    title = function_name + "-test-" + __import__("uuid").uuid4().hex[:6]
    proj = create_project(title=title, account=account)
    script_id = proj["scriptId"]

    manifest: dict[str, Any] = {
        "timeZone": "Etc/UTC",
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "executionApi": {"access": "MYSELF"},
    }
    if library_id and library_version is not None:
        manifest["dependencies"] = {
            "libraries": [{
                "libraryId": library_id,
                "version": library_version,
                "userSymbol": library_symbol,
                "developmentMode": False,
            }]
        }

    update_content(script_id, [
        {"name": "appsscript", "type": "JSON", "source": _json.dumps(manifest, ensure_ascii=False, indent=2)},
        {"name": "Code", "type": "SERVER_JS", "source": code},
    ], account=account)

    result = run_function(
        script_id=script_id,
        function_name=function_name,
        params=params,
        dev_mode=True,
        account=account,
    )
    result["script_id"] = script_id
    result["script_url"] = f"https://script.google.com/d/{script_id}/edit"

    if not keep_project:
        from src.tools import drive as _drive
        try:
            _drive.delete(script_id, account=account)
        except Exception:
            result["cleanup_failed"] = True
    return result


def run_smart(
    script_id: str | None = None,
    function_name: str = "main",
    params: list | None = None,
    spreadsheet_id: str | None = None,
    custom_menu_path: list[str] | None = None,
    account: str = DEFAULT_ACCOUNT,
    wait_after_menu_sec: int = 300,
) -> dict:
    """Cascade: scripts.run dev → scripts.run pinned → Playwright menu click.
    Designed to "just run this function" regardless of GCP project alignment.

    `script_id` runs scripts.run directly. If 403/404 (typical GCP project
    mismatch for bound scripts), falls back to spreadsheet + custom menu via
    Playwright. Provide at least one of (script_id, spreadsheet_id).

    Returns {ok, result | error, path_taken}.
    """
    attempts: list[dict] = []

    # Attempt 1: scripts.run dev mode (HEAD code)
    if script_id:
        try:
            r = run_function(script_id, function_name, params=params, dev_mode=True, account=account)
            if r.get("ok"):
                return {**r, "path_taken": "scripts.run dev"}
            attempts.append({"step": "scripts.run dev", "ok": False, "err": r.get("error_message")})
        except Exception as e:
            attempts.append({"step": "scripts.run dev", "err": str(e)[:200]})

        # Attempt 2: scripts.run with pinned deployment
        try:
            v = create_version(script_id, description="run_smart", account=account)
            create_deployment(script_id, v["versionNumber"], description="run_smart", account=account)
            r = run_function(script_id, function_name, params=params, dev_mode=False, account=account)
            if r.get("ok"):
                return {**r, "path_taken": "scripts.run pinned"}
            attempts.append({"step": "scripts.run pinned", "ok": False, "err": r.get("error_message")})
        except Exception as e:
            attempts.append({"step": "scripts.run pinned", "err": str(e)[:200]})

    # Attempt 3: Playwright menu click on the spreadsheet
    if spreadsheet_id and custom_menu_path:
        try:
            from src.tools import browser as _browser
            r = _browser.click_custom_menu(
                spreadsheet_id=spreadsheet_id,
                menu_path=custom_menu_path,
                headless=True,
                wait_after_click_sec=wait_after_menu_sec,
                timeout_sec=180,
            )
            return {"ok": True, "path_taken": "playwright menu", "click_info": r}
        except Exception as e:
            attempts.append({"step": "playwright menu", "err": str(e)[:200]})

    return {"ok": False, "attempts": attempts, "hint": "Provide custom_menu_path+spreadsheet_id for Playwright fallback"}


def triggers_install_one_shot(
    script_id: str,
    function_name: str,
    delay_minutes: int = 1,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Install a one-shot CLOCK trigger that runs `function_name` after
    `delay_minutes`. Uses scripts.run to install since Apps Script API has no
    direct trigger API. The script must have executionApi.access set + GCP
    projects aligned.

    Returns {triggered_function, fires_at_iso, ok}.
    """
    import datetime as _dt
    fire_at = (_dt.datetime.utcnow() + _dt.timedelta(minutes=delay_minutes)).isoformat() + "Z"
    install_code = f"""function __install_oneshot() {{
        ScriptApp.newTrigger("{function_name}")
            .timeBased()
            .after({delay_minutes * 60 * 1000})
            .create();
        return {{installed: true, function: "{function_name}", fires_in_min: {delay_minutes}}};
    }}"""
    # Push the install function, run it, then we leave it (harmless)
    try:
        edit_file(
            script_id=script_id,
            file_name="__OneShotInstaller",
            new_source=install_code,
            file_type="SERVER_JS",
            account=account,
        )
        r = run_function(script_id, "__install_oneshot", dev_mode=True, account=account)
        return {
            "ok": r.get("ok"),
            "triggered_function": function_name,
            "fires_at_iso": fire_at,
            "result": r.get("result") if r.get("ok") else r.get("error_message"),
        }
    except Exception as e:
        return {"ok": False, "triggered_function": function_name,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "hint": "Likely GCP project mismatch — use browser_set_script_gcp_project first"}


def triggers_list(script_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """List all installed triggers on `script_id`. Pushes a tiny enumerator
    function, runs it, returns the list. Requires executionApi + GCP alignment.
    """
    code = """function __list_triggers() {
        return ScriptApp.getProjectTriggers().map(function(t) {
            return {
                id: t.getUniqueId(),
                function: t.getHandlerFunction(),
                event_type: String(t.getEventType()),
                source: String(t.getTriggerSource())
            };
        });
    }"""
    try:
        edit_file(script_id=script_id, file_name="__TriggerLister", new_source=code, file_type="SERVER_JS", account=account)
        r = run_function(script_id, "__list_triggers", dev_mode=True, account=account)
        return {"ok": r.get("ok"), "triggers": r.get("result", []) if r.get("ok") else None, "error": r.get("error_message")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "hint": "Likely GCP project mismatch — use browser_set_script_gcp_project first"}


def triggers_remove(script_id: str, trigger_id: str | None = None, function_name: str | None = None,
                    account: str = DEFAULT_ACCOUNT) -> dict:
    """Remove triggers by ID or by handler function name. Returns
    {removed_count}.
    """
    code = (
        "function __remove_triggers() {\n"
        "  var removed = 0;\n"
        "  ScriptApp.getProjectTriggers().forEach(function(t) {\n"
        + (f"    if (t.getUniqueId() === {json.dumps(trigger_id)}) {{ ScriptApp.deleteTrigger(t); removed++; return; }}\n" if trigger_id else "")
        + (f"    if (t.getHandlerFunction() === {json.dumps(function_name)}) {{ ScriptApp.deleteTrigger(t); removed++; }}\n" if function_name else "")
        + "  });\n  return {removed_count: removed};\n}\n"
    )
    try:
        edit_file(script_id=script_id, file_name="__TriggerRemover", new_source=code, file_type="SERVER_JS", account=account)
        r = run_function(script_id, "__remove_triggers", dev_mode=True, account=account)
        return {"ok": r.get("ok"), "result": r.get("result") if r.get("ok") else r.get("error_message")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def create_deployment(
    script_id: str,
    version_number: int,
    description: str = "API exec",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create an API-executable deployment pinned to a specific version.
    Use this when you need `run_function` with `dev_mode=False` (i.e. pinned
    code, not HEAD). For HEAD execution use dev_mode=True and skip this.
    """
    return _service(account).projects().deployments().create(
        scriptId=script_id,
        body={
            "versionNumber": version_number,
            "manifestFileName": "appsscript",
            "description": description,
        },
    ).execute()


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


_BOUND_REGISTRY_PATH = None  # lazy import to avoid circular


def _bound_registry_path():
    global _BOUND_REGISTRY_PATH
    if _BOUND_REGISTRY_PATH is None:
        from src.config import DATA_DIR
        _BOUND_REGISTRY_PATH = DATA_DIR / "bound_scripts.json"
    return _BOUND_REGISTRY_PATH


def _bound_registry_load() -> dict[str, dict]:
    from src.json_store import read_json
    return read_json(_bound_registry_path(), {})


def _bound_registry_save(data: dict) -> None:
    from src.json_store import write_json
    write_json(_bound_registry_path(), data)


def register_bound_script(
    spreadsheet_id: str,
    script_id: str,
    account: str = DEFAULT_ACCOUNT,
    notes: str = "",
) -> dict:
    """Save a `spreadsheet_id → script_id` mapping so the agent can find the
    bound script later without enumerating Drive (which doesn't expose bound
    scripts). After one registration, get_bound_script_token resolves
    instantly. Use this when the user shares the script's URL or ID for a
    spreadsheet — typical workflow when first analyzing a new WB / finance
    report.

    Records: {spreadsheet_id: {script_id, account, registered_at, notes}}.
    """
    from src.json_store import now_iso_z
    reg = _bound_registry_load()
    reg[spreadsheet_id] = {
        "script_id": script_id,
        "account": account,
        "registered_at": now_iso_z(),
        "notes": notes,
    }
    _bound_registry_save(reg)
    return {"spreadsheet_id": spreadsheet_id, "script_id": script_id, "registered": True}


def list_bound_scripts() -> dict:
    """List all known `spreadsheet_id → script_id` mappings. Useful for the
    agent to recall which bound scripts it has been taught about."""
    return _bound_registry_load()


def resolve_bound_script(
    spreadsheet_id: str,
    account: str = DEFAULT_ACCOUNT,
    use_browser: bool = True,
) -> dict:
    """Resolve a spreadsheet to its bound script. Order of attempts:
      1. Local registry (instant; cached from prior calls)
      2. Drive enumeration via find_bound_script (rarely works — Drive API
         doesn't expose bound scripts, but kept in case Google changes its mind)
      3. Browser automation via Playwright (clicks Extensions → Apps Script
         in a real browser, reads the new tab's URL). Requires the browser
         profile to be logged in to Google.

    Returns {script_id, source, account} where source ∈ {registry, enumeration,
    browser}. Raises ValueError with guidance if everything fails.
    """
    reg = _bound_registry_load()
    if spreadsheet_id in reg:
        entry = reg[spreadsheet_id]
        return {"script_id": entry["script_id"], "source": "registry", "account": entry.get("account", account)}

    bound = find_bound_script(spreadsheet_id, account=account)
    if bound:
        register_bound_script(spreadsheet_id, bound[0]["script_id"], account=account, notes="auto-discovered (drive enum)")
        return {"script_id": bound[0]["script_id"], "source": "enumeration", "account": account}

    if use_browser:
        try:
            from src.tools import browser as _browser
            r = _browser.get_bound_script_id(spreadsheet_id, headless=True, timeout_sec=60)
            register_bound_script(spreadsheet_id, r["script_id"], account=account, notes="auto-discovered (playwright)")
            return {"script_id": r["script_id"], "source": "browser", "account": account}
        except Exception as e:
            raise ValueError(
                f"No bound script known for spreadsheet {spreadsheet_id}. Drive's API doesn't expose "
                f"bound scripts; Playwright fallback failed: {e}. Either run "
                f"src.tools.browser.login_interactive() to log in the browser profile, "
                f"or call apps_script_api_register_bound_script(spreadsheet_id='{spreadsheet_id}', script_id=...) "
                f"once after copying the ID from the Apps Script editor URL."
            )

    raise ValueError(
        f"No bound script known for spreadsheet {spreadsheet_id}. Drive's API doesn't expose "
        f"bound scripts. Either enable use_browser=True, or call "
        f"apps_script_api_register_bound_script(spreadsheet_id='{spreadsheet_id}', script_id=...) "
        f"once after copying the ID from the Apps Script editor URL."
    )


def get_bound_script_token(
    spreadsheet_id: str,
    function_name: str = "getToken",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Extract an API token from the Apps Script BOUND to `spreadsheet_id`.

    Convention used in many WB / financial-report spreadsheets: the bound
    script contains `function getToken() { return "<api-token>"; }` and every
    other init function calls it. This tool resolves the bound script (via
    registry or Drive enumeration), locates that function, and returns the
    string literal it returns.

    Returns {token, script_id, file_name, function_name}.
    Raises if no bound script known (with hint to register one), no such
    function, or the function body has no string-literal return (e.g. it pulls
    from ScriptProperties — use apps_script_api_run_function for that).
    """
    import re

    resolved = resolve_bound_script(spreadsheet_id, account=account)
    script_id = resolved["script_id"]
    # If registry told us a different account owns the script, use that
    effective_account = resolved.get("account", account)
    content = get_content(script_id, account=effective_account)

    for f in content.get("files", []):
        if f.get("type") != "SERVER_JS":
            continue
        src = f.get("source") or ""
        start, end = _find_function_span(src, function_name)
        if start is None:
            continue
        body = src[start:end]
        m = re.search(r"""return\s+(["'`])((?:\\.|(?!\1).)*)\1""", body, re.DOTALL)
        if m:
            return {
                "token": m.group(2),
                "script_id": script_id,
                "file_name": f.get("name"),
                "function_name": function_name,
                "bound_to_spreadsheet": spreadsheet_id,
                "source": resolved.get("source"),
            }
        # Found function but no string-literal return
        raise ValueError(
            f"Function {function_name!r} in bound script {script_id} doesn't return a "
            f"string literal — body starts:\n{body[:200]}\nUse apps_script_api_run_function "
            f"to actually invoke it if the token is computed at runtime."
        )

    files = [f.get("name") for f in content.get("files", [])]
    raise ValueError(
        f"Function {function_name!r} not found in any file of bound script {script_id}. "
        f"Files: {files}"
    )


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
