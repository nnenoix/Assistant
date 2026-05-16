"""Single source of truth for tool name → callable, schema, policy op.

Wraps each Python tool function as a claude-agent-sdk `@tool` and assembles them
into an in-process SDK MCP server. The agent loop registers this server with
ClaudeSDKClient and uses POLICY_OP_BY_TOOL to gate execution.
"""
import asyncio
import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from src import auth
from src.tools import apps_script, chats, drive, excel, gmail, local_fs, notes, people, sheets


MCP_SERVER_NAME = "gworkagent"
# Claude sees tools as: mcp__gworkagent__<tool_name>

_ACCOUNT_PROP = {
    "type": "string",
    "description": "OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases.",
}


def _tool(name, fn, policy_op, description, input_schema):
    """Build a tool spec. If `fn` accepts an `account` parameter, the schema
    is automatically augmented with an optional `account` field so Claude
    knows it can target a specific Google account."""
    accepts_account = "account" in fn.__code__.co_varnames[: fn.__code__.co_argcount]
    if accepts_account:
        input_schema = dict(input_schema)
        props = dict(input_schema.get("properties", {}))
        if "account" not in props:
            props["account"] = _ACCOUNT_PROP
        input_schema["properties"] = props
    return {
        "name": name,
        "fn": fn,
        "policy_op": policy_op,
        "schema": {"name": name, "description": description, "input_schema": input_schema},
    }


TOOLS = [
    # --- Drive ---
    _tool(
        "drive_list_files",
        drive.list_files,
        "drive.read",
        "List files in a Google Drive folder ordered by recently modified. folder_id='root' for My Drive root. Returns slim metadata (id, name, mimeType, modifiedTime) — for full info on a specific file use drive_get_metadata.",
        {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "default": "root"},
                "query": {"type": "string", "description": "Optional Drive query, e.g. \"name contains 'report'\""},
                "page_size": {"type": "integer", "description": "Max results to return, default 50, max 200"},
            },
        },
    ),
    _tool(
        "drive_get_metadata",
        drive.get_metadata,
        "drive.read",
        "Get metadata for a Drive file by id.",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_list_shared",
        drive.list_shared_with_me,
        "drive.read",
        "List files OTHER users have shared with the current account (Drive 'Shared with me'). Use this when the user asks about a file/folder/sheet that isn't in their own My Drive. Returns id, name, mimeType, modifiedTime, and owners.",
        {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer", "description": "Max results to return, default 50, max 200"},
            },
        },
    ),
    _tool(
        "drive_create_folder",
        drive.create_folder,
        "drive.create",
        "Create a new folder inside parent_id.",
        {"type": "object", "properties": {"parent_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["parent_id", "name"]},
    ),
    _tool(
        "drive_upload",
        drive.upload,
        "drive.create",
        "Upload a local file to Drive folder parent_id.",
        {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "parent_id": {"type": "string"},
                "name": {"type": "string"},
                "mime_type": {"type": "string"},
            },
            "required": ["local_path", "parent_id"],
        },
    ),
    _tool(
        "drive_download",
        drive.download,
        "drive.read",
        "Download a Drive file to a local path.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "dest_path": {"type": "string"}}, "required": ["file_id", "dest_path"]},
    ),
    _tool(
        "drive_update_content",
        drive.update_content,
        "drive.update",
        "Replace the content of an existing Drive file from a local file.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "local_path": {"type": "string"}, "mime_type": {"type": "string"}}, "required": ["file_id", "local_path"]},
    ),
    _tool(
        "drive_rename",
        drive.rename,
        "drive.update",
        "Rename a Drive file/folder.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["file_id", "new_name"]},
    ),
    _tool(
        "drive_move",
        drive.move,
        "drive.update",
        "Move a Drive file to a new parent folder.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_parent_id": {"type": "string"}}, "required": ["file_id", "new_parent_id"]},
    ),
    _tool(
        "drive_delete",
        drive.delete,
        "drive.delete",
        "Permanently delete a Drive file (no trash).",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_copy",
        drive.copy,
        "drive.create",
        "Copy a Drive file.",
        {"type": "object", "properties": {"file_id": {"type": "string"}, "new_name": {"type": "string"}, "parent_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_search",
        drive.search,
        "drive.read",
        "Search Drive files by name across everything the account can see (owned + shared with the user). Optional mime_type narrows by type — use friendly shortcuts: 'spreadsheet' (Google Sheets), 'doc' (Google Docs), 'folder', 'presentation', 'pdf', 'script' (Apps Script), 'form'. Example: search 'idealnight' with mime_type='spreadsheet' returns only Sheets named *idealnight*.",
        {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "mime_type": {"type": "string", "description": "Optional filter. Shortcuts: spreadsheet, doc, folder, presentation, pdf, script, form. Or pass a full mime string like 'application/vnd.google-apps.spreadsheet'."},
            },
            "required": ["name_contains"],
        },
    ),
    _tool(
        "drive_search_everywhere",
        drive.search_everywhere,
        "drive.read",
        "Run drive_search across EVERY configured Google account and aggregate. Use when the user says 'find X' without specifying which account, or 'check all my drives'. Returns {account_alias: [files]} so the agent can group results by source. Combines well with mime_type filtering.",
        {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "mime_type": {"type": "string", "description": "Optional. Same shortcuts as drive_search."},
            },
            "required": ["name_contains"],
        },
    ),
    _tool(
        "drive_name_patterns",
        drive.name_patterns,
        "drive.read",
        "STRUCTURAL ANALYSIS of file names matching a query — no file contents read. Returns recurring 2-3 letter uppercase codes (likely brand/project codes like SA, IN, RM), year tokens, doc-type words (ОПиУ, ДДС, Баланс), and frequent other words. **Call this FIRST** when the user asks 'what brands/projects/clients does X have', 'из чего состоит X', 'какие направления у X'. The answer is in the file NAMES — every code/word in the recurring buckets is part of the answer. List them ALL.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    ),
    _tool(
        "drive_name_patterns_everywhere",
        drive.name_patterns_everywhere,
        "drive.read",
        "drive_name_patterns aggregated across EVERY configured account. Use for structural questions when you don't know which account the entity belongs to. Reports per-account file counts and aggregates name analysis.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    ),
    # --- Sheets ---
    _tool(
        "sheets_read_range",
        sheets.read_range,
        "sheets.read",
        "Read a range from a Google Sheet. range example: 'Sheet1!A1:C100'.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id", "range"]},
    ),
    _tool(
        "sheets_write_range",
        sheets.write_range,
        "sheets.write",
        "Overwrite a range with values (list of rows). Formulas allowed.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
    ),
    _tool(
        "sheets_append_rows",
        sheets.append_rows,
        "sheets.write",
        "Append rows below existing data in the given range.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
    ),
    _tool(
        "sheets_clear_range",
        sheets.clear_range,
        "sheets.write",
        "Clear all values in a range.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id", "range"]},
    ),
    _tool(
        "sheets_create_spreadsheet",
        sheets.create_spreadsheet,
        "sheets.write",
        "Create a brand-new spreadsheet.",
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    ),
    _tool(
        "sheets_add_sheet",
        sheets.add_sheet,
        "sheets.write",
        "Add a new tab/sheet to an existing spreadsheet.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["spreadsheet_id", "title"]},
    ),
    _tool(
        "sheets_get_metadata",
        sheets.get_metadata,
        "sheets.read",
        "Get spreadsheet metadata: title and list of sheets/tabs.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}}, "required": ["spreadsheet_id"]},
    ),
    _tool(
        "sheets_summarize",
        sheets.summarize,
        "sheets.read",
        "Structural summary of a spreadsheet in ONE call: title, every sheet's name + grid size + header row + first N data rows (default 5). Use this FIRST when exploring an unfamiliar spreadsheet — it's much cheaper than reading each sheet separately.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sample_rows": {"type": "integer", "description": "How many data rows to include per sheet (default 5, max 50)."},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "sheets_find_in_spreadsheet",
        sheets.find_in_spreadsheet,
        "sheets.read",
        "Search a substring across EVERY sheet in a spreadsheet. Returns each match with its sheet name, A1 cell address, row/col indices, and the cell value. One call replaces many sheets_read_range calls when you need to locate something.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "query": {"type": "string"},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
            },
            "required": ["spreadsheet_id", "query"],
        },
    ),
    _tool(
        "sheets_find_and_replace",
        sheets.find_and_replace,
        "sheets.write",
        "Sheets-native find-and-replace via batchUpdate — one call, no read/write cycle. Auto-snapshots affected scope first (recoverable via sheets_rollback). Optional `sheet` to limit to one tab. Supports match_case, match_entire_cell, use_regex flags.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "sheet": {"type": "string", "description": "Optional tab name; if omitted, replaces in all sheets."},
                "match_case": {"type": "boolean"},
                "match_entire_cell": {"type": "boolean"},
                "use_regex": {"type": "boolean"},
            },
            "required": ["spreadsheet_id", "find", "replace"],
        },
    ),
    _tool(
        "sheets_list_backups",
        sheets.list_backups,
        "sheets.read",
        "List recent automatic snapshots taken before write/clear/find_and_replace operations on a spreadsheet. Each entry has snapshot_id, ts, range, op.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "limit": {"type": "integer", "description": "Default 20."},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "sheets_rollback",
        sheets.rollback,
        "sheets.write",
        "Restore a previously saved snapshot. If snapshot_id is omitted, uses the most recent snapshot. The affected range is cleared and rewritten with the snapshot's values. Use when the user says 'отмени' / 'верни как было'.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "snapshot_id": {"type": "string", "description": "Optional. Omit to use the most recent snapshot."},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "sheets_excel_to_sheets",
        sheets.excel_to_sheets,
        "sheets.write",
        "End-to-end pipeline: parse a local .xlsx file, create a new Google Spreadsheet, optionally move it into parent_folder_id, and copy every workbook sheet over (preserving sheet names). Returns the new spreadsheet_id and url. Replaces excel_parse + sheets_create_spreadsheet + multiple sheets_write_range calls.",
        {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "title": {"type": "string", "description": "Optional; defaults to the xlsx filename without extension."},
                "parent_folder_id": {"type": "string", "description": "Optional Drive folder to move the new spreadsheet into."},
            },
            "required": ["local_path"],
        },
    ),
    # --- Apps Script ---
    _tool(
        "apps_script_clone",
        apps_script.clone,
        "apps_script.edit",
        "Clone (or pull) an Apps Script project to local .data/scripts/.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_list_files",
        apps_script.list_files,
        "apps_script.edit",
        "List files in a cloned Apps Script project.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_read_file",
        apps_script.read_file,
        "apps_script.edit",
        "Read a file from a cloned Apps Script project.",
        {"type": "object", "properties": {"script_id": {"type": "string"}, "relpath": {"type": "string"}}, "required": ["script_id", "relpath"]},
    ),
    _tool(
        "apps_script_write_file",
        apps_script.write_file,
        "apps_script.edit",
        "Write a file in a cloned Apps Script project (local only, call apps_script_push to upload).",
        {"type": "object", "properties": {"script_id": {"type": "string"}, "relpath": {"type": "string"}, "content": {"type": "string"}}, "required": ["script_id", "relpath", "content"]},
    ),
    _tool(
        "apps_script_push",
        apps_script.push,
        "apps_script.edit",
        "Push local Apps Script project changes to Google.",
        {"type": "object", "properties": {"script_id": {"type": "string"}}, "required": ["script_id"]},
    ),
    _tool(
        "apps_script_run",
        apps_script.run_function,
        "apps_script.run",
        "Run a function in an Apps Script that has been deployed as API executable.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "function_name": {"type": "string"},
                "params": {"type": "array"},
            },
            "required": ["script_id", "function_name"],
        },
    ),
    # --- Excel ---
    _tool(
        "excel_parse",
        excel.parse_xlsx,
        "local.read",
        "Parse a local .xlsx file into row dicts. If `sheet` given, returns rows for that sheet only.",
        {"type": "object", "properties": {"path": {"type": "string"}, "sheet": {"type": "string"}}, "required": ["path"]},
    ),
    # --- Local FS ---
    _tool(
        "local_read_file",
        local_fs.read_file,
        "local.read",
        "Read a local text file.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    _tool(
        "local_write_file",
        local_fs.write_file,
        "local.write",
        "Write a local text file (creates parent dirs).",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    ),
    _tool(
        "local_list_dir",
        local_fs.list_dir,
        "local.read",
        "List entries in a local directory.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    # --- Auth (multi-account) ---
    _tool(
        "auth_list_accounts",
        auth.list_accounts,
        "auth.list",
        "List configured OAuth account aliases. Each alias corresponds to a Google account whose Drive/Sheets the agent can read and edit.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "auth_add_account",
        auth.add_account,
        "auth.add",
        "Authorize a new Google account under the given alias. Opens a browser on this machine; the user must log in and grant permissions. Blocks until the OAuth flow completes (~30s).",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Short alias for the new account, e.g. 'work', 'partner', or an email."},
            },
            "required": ["account"],
        },
    ),
    _tool(
        "auth_remove_account",
        auth.remove_account,
        "auth.remove",
        "Forget the stored token for the given account alias. Does NOT revoke the OAuth grant in the Google account itself.",
        {
            "type": "object",
            "properties": {"account": {"type": "string"}},
            "required": ["account"],
        },
    ),
    # --- Chat history (search your own past conversations) ---
    _tool(
        "chats_list_recent",
        chats.list_recent,
        "chats.read",
        "List recent saved chat sessions, newest first. Each entry has id, title (taken from the first user message), started_at, message_count. Use to remind the user (or yourself) what was discussed recently.",
        {"type": "object", "properties": {"limit": {"type": "integer", "description": "Default 30."}}},
    ),
    _tool(
        "chats_read",
        chats.read,
        "chats.read",
        "Read the full transcript of a specific past chat by id. The id format is a timestamp like '2026-05-16T14-30-00'.",
        {"type": "object", "properties": {"chat_id": {"type": "string"}}, "required": ["chat_id"]},
    ),
    _tool(
        "chats_search",
        chats.search,
        "chats.read",
        "Substring search across ALL saved chats. Returns matches with short snippets so you can decide which chat to read in full. Use when the user references prior work ('что мы делали с таблицей X на прошлой неделе').",
        {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
    ),
    _tool(
        "chats_search_semantic",
        chats.search_semantic,
        "chats.read",
        "SEMANTIC search across saved chats using local embeddings. Better than chats_search for fuzzy queries ('налоги' matches 'НДС', 'отчёт' matches 'reports'). Prefer this over chats_search unless you have an exact substring in mind. Falls back to substring if the embedding model isn't installed.",
        {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]},
    ),
    # --- Notes (persistent agent memory across sessions) ---
    _tool(
        "notes_add",
        notes.add,
        "notes.write",
        "Save a short note for future reference. Use for facts the user shares that you'll want later: IDs, preferences, recurring constants ('Лена 2026 НДС 5%', 'ID финального отчёта = 1AbC…'). Optional tag groups related notes. Always proactively save such facts.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tag": {"type": "string", "description": "Optional grouping tag like 'elena', 'taxes', 'ids'."},
            },
            "required": ["text"],
        },
    ),
    _tool(
        "notes_list",
        notes.list_notes,
        "notes.read",
        "List all stored notes, oldest first. Use to refresh your memory at the start of a session if the user references things you should know.",
        {"type": "object", "properties": {"limit": {"type": "integer", "description": "Default 50."}}},
    ),
    _tool(
        "notes_search",
        notes.search,
        "notes.read",
        "Find notes by substring across text and tag. Check this when the user asks about something they previously told you ('что я говорил про НДС?', 'какой был ID той презентации?').",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    _tool(
        "notes_search_semantic",
        notes.search_semantic,
        "notes.read",
        "SEMANTIC search across notes using local embeddings. Better at fuzzy retrieval ('налоги' → notes about НДС/налогообложении). Prefer this over notes_search unless looking for an exact substring. Falls back to substring if the embedding model isn't installed.",
        {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]},
    ),
    _tool(
        "notes_remove",
        notes.remove,
        "notes.write",
        "Delete a note by id. Use when the user explicitly asks to forget something.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    ),
    # --- People registry (name → account alias resolver) ---
    _tool(
        "people_list",
        people.list_people,
        "people.read",
        "List all people in the registry. Each entry binds one or more human names (and optionally an email) to a Google account alias.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "people_resolve",
        people.resolve,
        "people.read",
        "Resolve a free-text reference like 'Лена', 'у партнёра', or an email into matching registry entries. ALWAYS call this FIRST when the user mentions a person by name and you need to figure out which account's Drive/Sheets to operate on. Exactly one hit → use that .account. Multiple → ask the user to disambiguate. Zero hits → ask the user to confirm the person and call people_add.",
        {"type": "object", "properties": {"hint": {"type": "string"}}, "required": ["hint"]},
    ),
    _tool(
        "people_add",
        people.add,
        "people.write",
        "Register a person or merge new info into an existing entry. Bind multiple names (including nicknames and typo variants) to one account alias. Call this proactively when the user introduces a new person ('у Тани в drive есть таблица', 'мой коллега Pavel из work-аккаунта').",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "The OAuth alias (must already exist; create via auth_add_account first)."},
                "names": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "One name or a list of names/nicknames/variants."},
                "email": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["account", "names"],
        },
    ),
    _tool(
        "people_remove",
        people.remove,
        "people.write",
        "Drop a person from the registry by account alias.",
        {"type": "object", "properties": {"account": {"type": "string"}}, "required": ["account"]},
    ),
    # --- Gmail ---
    _tool(
        "gmail_search",
        gmail.search,
        "gmail.read",
        "Search emails using Gmail's native query syntax (same as the search bar): 'from:elena', 'has:attachment', 'subject:invoice', 'newer_than:7d'. Returns slim metadata (id, from, subject, date, snippet, labels).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "Default 20, max 100."},
            },
            "required": ["query"],
        },
    ),
    _tool(
        "gmail_get_message",
        gmail.get_message,
        "gmail.read",
        "Read a full message: headers, plain-text body (capped at 20k chars), and list of attachments. Returns body_text and the attachment list with attachment_ids you can download separately.",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
    _tool(
        "gmail_download_attachment",
        gmail.download_attachment,
        "gmail.read",
        "Save an attachment to a local path. Pass message_id and attachment_id from gmail_get_message.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "dest_path": {"type": "string"},
            },
            "required": ["message_id", "attachment_id", "dest_path"],
        },
    ),
    _tool(
        "gmail_create_draft",
        gmail.create_draft,
        "gmail.draft",
        "Create a DRAFT email (does NOT send). Always create a draft FIRST so the user can review; then call gmail_send_draft to actually send.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    ),
    _tool(
        "gmail_send_draft",
        gmail.send_draft,
        "gmail.send",
        "Send a draft created by gmail_create_draft. SEPARATE call so the user gets one explicit approval prompt before any email actually leaves the account.",
        {"type": "object", "properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]},
    ),
    _tool(
        "gmail_list_labels",
        gmail.list_labels,
        "gmail.read",
        "List Gmail labels (system + user-created). Useful for narrowing searches with 'label:foo'.",
        {"type": "object", "properties": {}},
    ),
]


BY_NAME = {t["name"]: t for t in TOOLS}
POLICY_OP_BY_TOOL = {t["name"]: t["policy_op"] for t in TOOLS}


# Max chars sent back to the model per tool call. ~12k chars ≈ 3-4k tokens.
# Tool outputs above this get cut with an instruction nudging the agent to
# narrow its next read. Keeps token bills sane on huge spreadsheets / Drive
# listings without losing the small-tool ergonomics.
MAX_TOOL_PAYLOAD = 12000


def _truncation_hint(name: str) -> str:
    """Per-tool guidance on how to read less. Saves the agent a guessing round."""
    if name.startswith("sheets_") and name != "sheets_summarize":
        return "Use sheets_summarize first to see structure, then read narrower ranges (e.g. 'Sheet1!A1:E50'), or sheets_find_in_spreadsheet for targeted lookups."
    if name.startswith("drive_"):
        return "Pass a smaller page_size, or filter with drive_search + mime_type."
    if name == "excel_parse":
        return "Call again with sheet=<name> to get one sheet at a time."
    if name.startswith("chats_") and name != "chats_search_semantic":
        return "Prefer chats_search_semantic with a focused query (returns top-k snippets, not full transcripts)."
    if name.startswith("notes_") and name != "notes_search_semantic":
        return "Prefer notes_search_semantic with a focused query."
    if name.startswith("gmail_") and name != "gmail_get_message":
        return "Narrow the Gmail query (add subject:/from:/newer_than:7d), reduce max_results."
    return "Re-call this tool with narrower input."


def _wrap_for_sdk(spec):
    """Decorate a sync tool fn as an async @tool that returns SDK-compatible output."""
    name = spec["name"]
    fn = spec["fn"]
    description = spec["schema"]["description"]
    input_schema = spec["schema"]["input_schema"]

    @tool(name, description, input_schema)
    async def wrapped(args):
        try:
            result = await asyncio.to_thread(fn, **args)
            if result is None:
                return {"content": [{"type": "text", "text": "(no output)"}]}
            payload = json.dumps(result, default=str, ensure_ascii=False)
            if len(payload) > MAX_TOOL_PAYLOAD:
                cut = MAX_TOOL_PAYLOAD - 400
                hint = _truncation_hint(name)
                payload = (
                    payload[:cut]
                    + f"\n\n…[TRUNCATED at {cut:,}/{len(payload):,} chars to save tokens. "
                    f"{hint}]"
                )
            return {"content": [{"type": "text", "text": payload}]}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "is_error": True,
            }
    return wrapped


def build_sdk_mcp_server():
    """Construct the in-process SDK MCP server with all 28 tools."""
    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version="1.0.0",
        tools=[_wrap_for_sdk(t) for t in TOOLS],
    )
