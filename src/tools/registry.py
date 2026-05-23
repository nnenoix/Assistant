"""Single source of truth for tool name → callable, schema, policy op.

Wraps each Python tool function as a claude-agent-sdk `@tool` and assembles them
into an in-process SDK MCP server. The agent loop registers this server with
ClaudeSDKClient and uses POLICY_OP_BY_TOOL to gate execution.
"""
import asyncio
import json
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from src import auth
from src.tools import (
    _idempotency, _quota,
    aliases, analytics, analytics_local, apps_script, apps_script_api,
    bank_parser, browser, calendar, chats, cloud_logging, contacts, docs,
    drive, edo, excel, external, file_analyze, file_extract, forms, gcp,
    gmail, infra, local_fs, logistics, macros, messaging, mlhelpers,
    moysklad, notes, onec, ozon, payments, pdf_gen, reply_check, reports,
    self_heal, service, sheets, slides, social,
    tasks as gtasks, translation, verify, vision, watcher, wb, web, yamarket,
)
# `tasks` aliased to gtasks because the bare name collides with pytest's
# internal `tasks` discovery in some contexts. Use gtasks.* throughout.

# Optional OpenTelemetry trace API — falls back to `None` when the package
# isn't installed so `_wrap_for_sdk`'s hot path stays lazy-import free.
# Status/StatusCode also imported at module top so the exception path
# doesn't pay an import cost on every failure.
try:
    from opentelemetry import trace as _otel_trace  # type: ignore
    from opentelemetry.trace import Status as _OtelStatus, StatusCode as _OtelStatusCode  # type: ignore
except Exception:  # pragma: no cover — opentelemetry not installed
    _otel_trace = None
    _OtelStatus = None
    _OtelStatusCode = None

# Prometheus-format metrics. Always available — the module has zero
# external dependencies. Hooked at the tool wrapper's finally block so
# every invocation increments call counters and the latency histogram.
from src import metrics as _metrics

# Tenant ContextVar accessor — read once per wrapped call to stamp the
# OTel span. Module-level import keeps the hot path lazy-import free.
from src.tenancy import current_tenant_id as _current_tenant_id


MCP_SERVER_NAME = "gworkagent"
# Claude sees tools as: mcp__gworkagent__<tool_name>

_ACCOUNT_PROP = {
    "type": "string",
    "description": "OAuth account alias (default 'main'). Call auth_list_accounts to see available aliases.",
}

# For drive multi-account tools: account can be alias / "*" / list of aliases.
_ACCOUNT_PROP_MULTI = {
    "oneOf": [
        {"type": "string", "description": "Alias, or '*' for every configured account."},
        {"type": "array", "items": {"type": "string"}, "description": "Explicit list of aliases — runs the call across each and aggregates."},
    ],
    "description": "OAuth account: single alias, '*' (all), or list of aliases.",
}


def _derive_category(name: str) -> str:
    """Default category from the tool name's first underscore-prefix.
    `sheets_read_range` → `sheets`; `gcp_enable_api` → `gcp`. Falls back
    to `misc` if no underscore."""
    if "_" in name:
        return name.split("_", 1)[0]
    return "misc"


# Verb → (readOnly, destructive, idempotent) hints. Verb is the part of
# `policy_op` after the dot. MCP annotations spec (2025-03-26) treats these
# as advisory hints — clients use them to gate confirmation prompts and
# parallelize read-only calls. Without these every tool defaults to
# destructive+open-world by convention, which makes the UX worse.
_VERB_HINTS: dict[str, tuple[bool, bool, bool]] = {
    # read-only, never modifies, repeatable
    "read":    (True,  False, True),
    "list":    (True,  False, True),
    "get":     (True,  False, True),
    "search":  (True,  False, True),
    "diff":    (True,  False, True),
    "test":    (True,  False, True),
    # writes — OVERWRITE existing state (MCP spec: not "additive only" → destructive).
    # Idempotent because same args yield same final state on replay.
    "write":   (False, True,  True),
    "update":  (False, True,  True),
    "set":     (False, True,  True),
    "edit":    (False, True,  True),
    "replace": (False, True,  True),
    # creators / appenders — each call produces a new entity
    "draft":   (False, False, False),
    "append":  (False, False, False),
    "add":     (False, False, False),
    "create":  (False, False, False),
    # destructive — modifies/removes existing state
    "delete":  (False, True,  True),   # second delete is no-op → idempotent
    "remove":  (False, True,  True),
    "send":    (False, True,  False),  # outbound message, irreversible per call
    "run":     (False, True,  False),  # runs arbitrary code → side effects
    "commit":  (False, True,  False),
    "revert":  (False, True,  False),
}

# Domains that don't touch external services. Everything else is treated as
# `openWorld=true` (the MCP-spec default for unspecified tools).
_LOCAL_DOMAINS = frozenset({"local", "self", "aliases", "notes", "chats"})

# Name tokens that override an "idempotent write" verb's idempotency hint.
# Necessary because some tools share a policy_op for access control but
# differ semantically: `sheets_append_rows` is `sheets.write` for ACL but
# isn't idempotent (each call appends new rows). `gmail_create_draft` is
# `gmail.draft` (additive but each call makes a new draft), etc.
_NON_IDEMPOTENT_NAME_TOKENS = ("_append", "_create", "_add", "_send", "_make")


def _annotations_for(policy_op: str | None, name: str | None = None) -> ToolAnnotations | None:
    """Derive MCP `ToolAnnotations` from the policy_op string. Returns None
    when the verb isn't recognised so the SDK falls back to its defaults.

    `name` lets us downgrade `idempotentHint=True` to False for tools whose
    name embeds a non-idempotent verb (`*_append_*`, `*_create_*`, ...)
    even when their policy_op is the broader `write`/`edit`/etc."""
    if not policy_op or "." not in policy_op:
        return None
    domain, _, verb = policy_op.partition(".")
    hint = _VERB_HINTS.get(verb)
    if hint is None:
        return None
    read_only, destructive, idempotent = hint
    open_world = domain not in _LOCAL_DOMAINS
    if idempotent and name and any(tok in name for tok in _NON_IDEMPOTENT_NAME_TOKENS):
        idempotent = False
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


_IDEMPOTENCY_KEY_PROP = {
    "type": "string",
    "description": (
        "Optional client-supplied idempotency key (Stripe-style). On retry "
        "with the same key + same args within 24h, the cached response is "
        "replayed and the tool is NOT re-executed. Use to safely retry "
        "destructive or non-idempotent calls after a flaky network. Recommended "
        "format: UUIDv4."
    ),
}

_DRY_RUN_PROP = {
    "type": "boolean",
    "default": False,
    "description": (
        "If true, do not execute. Return a preview describing what would "
        "happen (`{dry_run: true, plan: {...}}`). Use to verify a destructive "
        "call's intent before letting it run for real. Tools that haven't "
        "implemented a native preview return a stub with the resolved args."
    ),
}


def _tool(
    name, fn, policy_op, description, input_schema,
    category: str | None = None,
    annotations: ToolAnnotations | None = None,
):
    """Build a tool spec. If `fn` accepts an `account` parameter, the schema
    is automatically augmented with an optional `account` field so Claude
    knows it can target a specific Google account.

    For tools whose annotations declare `idempotentHint=False`, an optional
    `idempotency_key` parameter is appended to the schema so callers can
    deduplicate retries (see `src/tools/_idempotency.py`).

    `category` is used for opt-in dynamic tool filtering. If omitted, derived
    from the tool name's prefix.

    `annotations` lets callers override the policy_op-derived MCP hints
    (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`).
    Defaults are computed from policy_op via `_annotations_for`.
    """
    annot = annotations or _annotations_for(policy_op, name)
    fn_varnames = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    accepts_account = "account" in fn_varnames
    accepts_dry_run = "dry_run" in fn_varnames
    needs_idempotency_key = (
        annot is not None
        and annot.idempotentHint is False
        and not annot.readOnlyHint
    )
    is_destructive = annot is not None and annot.destructiveHint
    supports_dry_run = is_destructive  # destructive tools expose `dry_run` flag
    if accepts_account or needs_idempotency_key or supports_dry_run:
        input_schema = dict(input_schema)
        props = dict(input_schema.get("properties", {}))
        if accepts_account and "account" not in props:
            props["account"] = _ACCOUNT_PROP
        if needs_idempotency_key and "idempotency_key" not in props:
            props["idempotency_key"] = _IDEMPOTENCY_KEY_PROP
        if supports_dry_run and "dry_run" not in props:
            props["dry_run"] = _DRY_RUN_PROP
        input_schema["properties"] = props
    return {
        "name": name,
        "fn": fn,
        "policy_op": policy_op,
        "category": category or _derive_category(name),
        "annotations": annot,
        "supports_idempotency": needs_idempotency_key,
        "supports_dry_run": supports_dry_run,
        "native_dry_run": accepts_dry_run,
        "schema": {"name": name, "description": description, "input_schema": input_schema},
    }


TOOLS = [
    # --- Drive ---
    _tool(
        "drive_list_files",
        drive.list_files,
        "drive.read",
        "List files in a Drive folder, newest first. folder_id='root' for My Drive root. `account` accepts alias / '*' / list of aliases for multi-account fan-out. Returns {files, _meta:{truncated, ...}}. `response_format='concise'` (default) returns id+name+mimeType+modifiedTime; 'detailed' adds owners+size+parents+webViewLink.",
        {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "default": "root"},
                "query": {"type": "string", "description": "Optional Drive query, e.g. \"name contains 'report'\""},
                "page_size": {"type": "integer", "description": "Max results, default 50, max 200"},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
                "account": _ACCOUNT_PROP_MULTI,
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
        "List files OTHER users shared with this account ('Shared with me'). Use when the file isn't in user's own My Drive. `account` accepts alias / '*' / list. Returns id, name, mimeType, modifiedTime, owners.",
        {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer", "description": "Max results, default 50, max 200"},
                "account": _ACCOUNT_PROP_MULTI,
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
        "Search Drive by name (owned + shared). mime_type shortcuts: spreadsheet|doc|folder|presentation|pdf|script|form. `account` accepts alias / '*' / list of aliases. Returns {files, _meta:{truncated, empty_reason}}. page_size default 50, max 200.",
        {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "mime_type": {"type": "string", "description": "Optional filter. Shortcuts: spreadsheet, doc, folder, presentation, pdf, script, form. Or full mime string."},
                "account": _ACCOUNT_PROP_MULTI,
            },
            "required": ["name_contains"],
        },
    ),
    _tool(
        "drive_name_patterns",
        drive.name_patterns,
        "drive.read",
        "Structural analysis of file names matching a query — no contents read. Returns recurring codes, years, doc-types, frequent words. Call FIRST for 'из чего состоит X'. `account` accepts alias / '*' / list of aliases.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "account": _ACCOUNT_PROP_MULTI,
            },
            "required": ["query"],
        },
    ),
    # --- Drive sharing + history (Phase 4) ---
    _tool(
        "drive_list_permissions",
        drive.list_permissions,
        "drive.read",
        "List who has access to a Drive file. Returns {permissions: [{id, type, role, emailAddress, displayName}], _meta}. Use before share/revoke to know current state.",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_share",
        drive.share,
        "drive.write",
        "Grant `email` access to `file_id` at `role` level. role = reader (view), commenter (view+comment), writer (edit), owner (transfers ownership, see drive_transfer_ownership). `notify=True` sends Google's default email; pass `message` to customize.",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "email": {"type": "string"},
                "role": {"type": "string", "description": "reader|commenter|writer|owner"},
                "notify": {"type": "boolean"},
                "message": {"type": "string"},
            },
            "required": ["file_id", "email"],
        },
    ),
    _tool(
        "drive_revoke_permission",
        drive.revoke_permission,
        "drive.write",
        "Revoke a permission by its id (get it from drive_list_permissions).",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "permission_id": {"type": "string"},
            },
            "required": ["file_id", "permission_id"],
        },
    ),
    _tool(
        "drive_transfer_ownership",
        drive.transfer_ownership,
        "drive.write",
        "Transfer ownership to `new_owner_email`. For consumer Gmail accounts the receiver gets a pending-ownership notification they must accept (pending_owner=True in the response signals this).",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "new_owner_email": {"type": "string"},
            },
            "required": ["file_id", "new_owner_email"],
        },
    ),
    _tool(
        "drive_list_revisions",
        drive.list_revisions,
        "drive.read",
        "List version history of a Drive file. Each revision has id, modifiedTime, lastModifyingUser, size, mimeType. For native Google formats Google auto-saves revisions; for binary uploads each update is a revision.",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_download_revision",
        drive.download_revision,
        "drive.read",
        "Download a binary revision to `dest_path`. Works on PDFs, images, .xlsx uploads. Does NOT work on native Google formats (Sheets/Docs/Slides) — for those use revision metadata + Drive UI's version history.",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "revision_id": {"type": "string"},
                "dest_path": {"type": "string"},
            },
            "required": ["file_id", "revision_id", "dest_path"],
        },
    ),
    _tool(
        "drive_add_comment",
        drive.add_comment,
        "drive.write",
        "Add a comment to a Drive file (works on Docs/Sheets/Slides/PDFs). For anchored comments pass the JSON `anchor` string Drive expects; otherwise the comment is file-level.",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "content": {"type": "string"},
                "anchor": {"type": "string"},
            },
            "required": ["file_id", "content"],
        },
    ),
    _tool(
        "drive_list_comments",
        drive.list_comments,
        "drive.read",
        "List comments on a file. By default skips resolved comments; pass `include_resolved=true` to include them.",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "include_resolved": {"type": "boolean"},
            },
            "required": ["file_id"],
        },
    ),
    _tool(
        "drive_resolve_comment",
        drive.resolve_comment,
        "drive.write",
        "Mark a comment as resolved (Drive's 'Done' button).",
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "comment_id": {"type": "string"},
            },
            "required": ["file_id", "comment_id"],
        },
    ),
    _tool(
        "drive_list_trash",
        drive.list_trash,
        "drive.read",
        "List files currently in the trash. Returns {files, _meta:{truncated}}.",
        {
            "type": "object",
            "properties": {
                "page_size": {"type": "integer"},
            },
        },
    ),
    _tool(
        "drive_restore_from_trash",
        drive.restore_from_trash,
        "drive.write",
        "Restore a trashed file (sets trashed=false).",
        {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    ),
    _tool(
        "drive_empty_trash",
        drive.empty_trash,
        "drive.delete",
        "PERMANENTLY delete EVERYTHING in the trash. Irreversible — confirm with the user first.",
        {"type": "object", "properties": {}},
    ),
    # --- Sheets ---
    _tool(
        "sheets_read_range",
        sheets.read_range,
        "sheets.read",
        "Read a range. range example: 'Sheet1!A1:C100'. Returns {values, _meta:{range_read, empty_reason, value_mode}}. `formatted=true` → values as displayed (e.g. '3 087 967 ₽'); default raw. For finding a known metric prefer `sheets_metric_lookup`.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "formatted": {"type": "boolean", "description": "Return values as displayed (currency symbols, date formats) instead of raw."},
            },
            "required": ["spreadsheet_id", "range"],
        },
    ),
    _tool(
        "sheets_batch_read",
        sheets.batch_read,
        "sheets.read",
        "Read MULTIPLE ranges in ONE HTTP (values.batchGet). E.g. ['Sheet1!B45', 'Sheet2!C12:C18']. Returns {per_range: [{range, values, row_count, empty}], _meta}. Prefer over many read_range when consolidating across tabs.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "ranges": {"type": "array", "items": {"type": "string"}, "description": "A1 ranges. Empty list returns empty per_range."},
                "formatted": {"type": "boolean"},
            },
            "required": ["spreadsheet_id", "ranges"],
        },
    ),
    _tool(
        "sheets_list_named_ranges",
        sheets.list_named_ranges,
        "sheets.read",
        "List named ranges in a spreadsheet. Call FIRST for metric lookup — if `Чистая_прибыль_Год` exists, read it directly. Returns {named_ranges: [{name, sheet, range, named_range_id}], _meta}.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}}, "required": ["spreadsheet_id"]},
    ),
    _tool(
        "sheets_read_named_range",
        sheets.read_named_range,
        "sheets.read",
        "Read the values stored at a named range by name. Returns {values, _meta:{name, range_read}}. The most reliable way to look up a labelled metric — no fuzzy match, no risk of grabbing the wrong row.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "name": {"type": "string", "description": "Named range name, e.g. 'Чистая_прибыль_Год'."},
                "formatted": {"type": "boolean"},
            },
            "required": ["spreadsheet_id", "name"],
        },
    ),
    _tool(
        "sheets_create_named_range",
        sheets.create_named_range,
        "sheets.write",
        "Define a new named range in a spreadsheet. range example: 'Sheet1!B45' (single cell) or 'Sheet1!B45:B45'. Useful when tidying up a workbook so future agents (and users) can refer to key metrics by name.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "name": {"type": "string"},
                "range": {"type": "string"},
            },
            "required": ["spreadsheet_id", "name", "range"],
        },
    ),
    _tool(
        "sheets_duplicate_sheet",
        sheets.duplicate_sheet,
        "sheets.write",
        "Duplicate a sheet/tab inside the SAME spreadsheet. `source_sheet` accepts the tab title (string) OR the numeric sheetId. Returns {new_sheet_id, title, index}. For copying between different spreadsheets use sheets_copy_sheet_to.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "source_sheet": {"type": ["string", "integer"], "description": "Tab title OR numeric sheetId."},
                "new_name": {"type": "string"},
            },
            "required": ["spreadsheet_id", "source_sheet", "new_name"],
        },
    ),
    _tool(
        "sheets_copy_sheet_to",
        sheets.copy_sheet_to,
        "sheets.write",
        "Copy a tab from one spreadsheet to ANOTHER spreadsheet. `source_sheet` accepts the tab title (string) OR the numeric sheetId. Destination gets a new sheet titled 'Copy of …' — rename via batchUpdate if needed.",
        {
            "type": "object",
            "properties": {
                "source_spreadsheet_id": {"type": "string"},
                "source_sheet": {"type": ["string", "integer"], "description": "Tab title OR numeric sheetId."},
                "dest_spreadsheet_id": {"type": "string"},
            },
            "required": ["source_spreadsheet_id", "source_sheet", "dest_spreadsheet_id"],
        },
    ),
    _tool(
        "sheets_set_format",
        sheets.set_format,
        "sheets.write",
        "Apply formatting to a range. `preset` ∈ {currency_rub, currency_rub_int, currency_usd, currency_eur, percent, percent_int, date_iso, date_ru, datetime_ru, number_2dp, number_int, text} OR raw `number_format` dict. Also `background_color`={r,g,b: 0..1}, `text_format`={bold, italic, fontSize}.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "preset": {"type": "string", "description": "currency_rub | currency_rub_int | percent | date_ru | etc."},
                "number_format": {"type": "object"},
                "background_color": {"type": "object"},
                "text_format": {"type": "object"},
            },
            "required": ["spreadsheet_id", "range"],
        },
    ),
    _tool(
        "sheets_freeze",
        sheets.freeze,
        "sheets.write",
        "Freeze N rows + M cols of a sheet so they stay pinned while scrolling. `sheet` can be the tab title or numeric sheetId. Most common: rows=1 to pin the header row.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": ["string", "integer"]},
                "rows": {"type": "integer"},
                "cols": {"type": "integer"},
            },
            "required": ["spreadsheet_id", "sheet"],
        },
    ),
    _tool(
        "sheets_merge_cells",
        sheets.merge_cells,
        "sheets.write",
        "Merge a rectangular range. merge_type: MERGE_ALL (default — one big cell), MERGE_COLUMNS (rows merge per-column), MERGE_ROWS (cols merge per-row).",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "merge_type": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range"],
        },
    ),
    _tool(
        "sheets_unmerge_cells",
        sheets.unmerge_cells,
        "sheets.write",
        "Undo merges inside a range.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id", "range"]},
    ),
    _tool(
        "sheets_set_data_validation",
        sheets.set_data_validation,
        "sheets.write",
        "Attach a validation rule to a range. kind=`dropdown` (needs `values` list), `number_between` (needs min_value+max_value), `checkbox`, or `remove` (clears existing rules).",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "kind": {"type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
                "min_value": {"type": "number"},
                "max_value": {"type": "number"},
                "strict": {"type": "boolean"},
                "show_dropdown": {"type": "boolean"},
            },
            "required": ["spreadsheet_id", "range", "kind"],
        },
    ),
    _tool(
        "sheets_set_conditional_format",
        sheets.set_conditional_format,
        "sheets.write",
        "Add a conditional-format rule. condition: `negatives_red`, `positives_green`, `less_than` (needs threshold), `greater_than` (needs threshold), `text_contains` (needs text). Optional custom `color` dict {red, green, blue: 0..1}.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "condition": {"type": "string"},
                "color": {"type": "object"},
                "threshold": {"type": "number"},
                "text": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range", "condition"],
        },
    ),
    _tool(
        "sheets_create_chart",
        sheets.create_chart,
        "sheets.write",
        "Insert a chart. chart_type: line|bar|column|pie|area|scatter. domain_range = A1 for X axis (one column). series_ranges = list of A1 ranges, one per Y series. position_row/col control where the chart is anchored. Returns {chart_id}.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": ["string", "integer"]},
                "chart_type": {"type": "string"},
                "title": {"type": "string"},
                "domain_range": {"type": "string"},
                "series_ranges": {"type": "array", "items": {"type": "string"}},
                "position_sheet": {"type": ["string", "integer"]},
                "position_row": {"type": "integer"},
                "position_col": {"type": "integer"},
            },
            "required": ["spreadsheet_id", "sheet", "chart_type", "title", "domain_range", "series_ranges"],
        },
    ),
    _tool(
        "sheets_create_pivot",
        sheets.create_pivot,
        "sheets.write",
        "Create a pivot table. source_range must include a header row. rows/columns are lists of header NAMES (not letters). values is list of {column: <header>, aggregate: SUM|AVERAGE|COUNT|MAX|MIN, name?: <label>}. If dest_sheet is omitted, a new tab is created.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "source_range": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "string"}},
                "columns": {"type": "array", "items": {"type": "string"}},
                "values": {"type": "array", "items": {"type": "object"}},
                "dest_sheet": {"type": ["string", "integer"]},
                "dest_cell": {"type": "string"},
            },
            "required": ["spreadsheet_id", "source_range", "rows"],
        },
    ),
    _tool(
        "sheets_add_protected_range",
        sheets.add_protected_range,
        "sheets.write",
        "Protect a range from edits. `warning_only=true` → confirm prompt but lets edits through; default false blocks all but listed `editors` (emails). Omit editors → only owner. Returns {protected_range_id} for later removal.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "description": {"type": "string"},
                "warning_only": {"type": "boolean"},
                "editors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["spreadsheet_id", "range"],
        },
    ),
    _tool(
        "sheets_list_protected_ranges",
        sheets.list_protected_ranges,
        "sheets.read",
        "List every protected range in a spreadsheet. Returns {protected_ranges: [{protected_range_id, description, warning_only, sheet, range, editors}], _meta}.",
        {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}}, "required": ["spreadsheet_id"]},
    ),
    _tool(
        "sheets_remove_protected_range",
        sheets.remove_protected_range,
        "sheets.write",
        "Remove a protected range by its numeric protectedRangeId (get it from sheets_list_protected_ranges).",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "protected_range_id": {"type": "integer"},
            },
            "required": ["spreadsheet_id", "protected_range_id"],
        },
    ),
    _tool(
        "sheets_set_cell_note",
        sheets.set_cell_note,
        "sheets.write",
        "Attach a hover-shown 'note' to a cell or range. Distinct from Drive comments (drive_add_comment is for file-level discussion threads). Pass `note=''` to clear.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range", "note"],
        },
    ),
    _tool(
        "sheets_get_cell_notes",
        sheets.get_cell_notes,
        "sheets.read",
        "Read notes attached to cells in a range. Returns {notes: 2D array of strings/None, _meta}. Empty cells return None at that position.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range"],
        },
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
        "ONE-call structural summary: each sheet's name + grid + header + N sample rows (default 5, max 50). `_meta.data_rows_estimate` = real extent; `grid.rows` is sheet DIMENSION (padded). `_meta.is_sample=true` → slice only. Use FIRST; then narrow with sheets_metric_lookup.",
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
        "Substring search across EVERY sheet. Returns {matches: [{sheet, cell, row, col, value, row_label?, col_label?}], _meta}. `with_labels=true` attaches col A + row 1 labels. For metric+period lookup prefer sheets_metric_lookup.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "query": {"type": "string"},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
                "with_labels": {"type": "boolean", "description": "When true, each match also carries row_label (col A) and col_label (row 1) — use to verify the metric/period the cell actually belongs to."},
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
        "End-to-end: parse .xlsx → create new Google Spreadsheet → optionally move to parent_folder_id → copy every sheet (names preserved). Returns spreadsheet_id + url. Replaces excel_parse + create_spreadsheet + N write_range calls.",
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
    _tool(
        "sheets_query",
        sheets.query,
        "sheets.write",
        "Server-side aggregation: SELECT/WHERE/GROUP BY/ORDER BY/LIMIT against a range. **For SUM/COUNT/AVG/GROUP/topN over millions of rows.** For a single metric+period cell prefer sheets_metric_lookup. 10k-row cap: `_meta.truncated=true` → narrow WHERE. policy=write (hidden temp sheet). `response_format=\"concise\"` (default) trims to first 50 rows of the result; pass \"detailed\" for the full grid.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "source_range": {"type": "string", "description": "Range like 'Orders!A:M' or 'Orders' (whole sheet). First row is treated as headers."},
                "sql": {"type": "string", "description": "QUERY language, e.g. 'SELECT A, SUM(C) WHERE B > 100 GROUP BY A ORDER BY SUM(C) DESC LIMIT 20'"},
                "response_format": {
                    "type": "string",
                    "enum": ["concise", "detailed"],
                    "default": "concise",
                    "description": "concise: first 50 result rows (default, token-efficient). detailed: full grid up to 10k rows.",
                },
            },
            "required": ["spreadsheet_id", "source_range", "sql"],
        },
    ),
    _tool(
        "sheets_profile",
        sheets.profile,
        "sheets.write",
        "Server-side column stats: name, non_blank, blank, distinct, type, top_5, min/max/avg for numeric. No raw row fetch. Use BEFORE reading large/unfamiliar sheets. policy=write (temp sheet, auto-cleaned).",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string", "description": "Tab name."},
            },
            "required": ["spreadsheet_id", "sheet"],
        },
    ),
    _tool(
        "sheets_iter_rows",
        sheets.iter_rows,
        "sheets.read",
        "Paginated read: `chunk_size` rows from data row `offset` (0-based, excludes header). Returns {rows, offset, next_offset, has_more}. Only when per-row inspection is needed and QUERY/PROFILE/SCRIPT won't work. Loop until has_more=False. Default chunk=200, max 5000.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string", "description": "Just the tab name (no '!' or range)."},
                "offset": {"type": "integer", "description": "0-based data row offset (skip header automatically)."},
                "chunk_size": {"type": "integer", "description": "Default 200, max 5000."},
                "columns": {"type": "string", "description": "Column range, default 'A:ZZ'."},
            },
            "required": ["spreadsheet_id", "sheet"],
        },
    ),
    _tool(
        "apps_script_oneshot",
        macros.apps_script_oneshot,
        "apps_script.run",
        "Run a one-off Apps Script: creates standalone script, pushes code, runs via clasp, returns result. For tasks too complex for QUERY/find_replace/iter_rows — full SpreadsheetApp/Drive API access, can read+mutate many files. First call may fail 'not deployed' (one-time per-script setup); response then has script_url for manual deploy.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Full Apps Script source, must define a function named `function_name` (default 'main') with no required arguments."},
                "function_name": {"type": "string", "description": "Default 'main'."},
                "keep_project": {"type": "boolean", "description": "Default false. Set true to preserve the local clone for re-runs."},
                "alias": {"type": "string", "description": "Optional alias for the project — useful when keep_project=true so you can re-run via apps_script_run."},
            },
            "required": ["code"],
        },
    ),
    # --- Apps Script API (direct, account-aware, no clasp) ---
    _tool(
        "apps_script_api_get_content",
        apps_script_api.get_content,
        "apps_script.edit",
        "Read FULL source (all files inline) of an Apps Script project. Often >100k chars → gets truncated. Prefer apps_script_api_list_files + apps_script_api_get_file (staged to disk). Use only when you need everything in memory.",
        {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_list_files",
        apps_script_api.list_files,
        "apps_script.edit",
        "List file names + types + sizes (lines, bytes) of an Apps Script project. NO source content — won't blow the token cap. Use this FIRST to see what's in the project, then fetch the specific file you care about with apps_script_api_get_file.",
        {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_get_file",
        apps_script_api.get_file,
        "apps_script.edit",
        "Fetch ONE file, STAGE locally to `.data/staging/<script_id>/<file_name>.gs`. Returns staged_path + preview. Read staged via local_read_file (offset/limit for big files), edit, push back via apps_script_api_edit_file. Canonical local-first read path.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "file_name": {"type": "string", "description": "Without extension. E.g. '2.3 Финансы с датой отчета'."},
            },
            "required": ["script_id", "file_name"],
        },
    ),
    _tool(
        "apps_script_api_get_bound_script_token",
        apps_script_api.get_bound_script_token,
        "apps_script.edit",
        "Extract an API token (e.g. WB) from the bound script of `spreadsheet_id`. Convention: `function getToken() { return \"<token>\"; }`. Returns {token, script_id, file_name, function_name}. Auto-resolves bound script; on miss tells you to register via apps_script_api_register_bound_script.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "function_name": {"type": "string", "default": "getToken"},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "apps_script_api_register_bound_script",
        apps_script_api.register_bound_script,
        "apps_script.edit",
        "Record which script_id is bound to a spreadsheet (Drive API doesn't enumerate bound scripts). Get script_id from `script.google.com/d/<SCRIPT_ID>/edit`. After registration, get_bound_script_token + resolve_bound_script work instantly.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "script_id": {"type": "string"},
                "notes": {"type": "string", "default": "", "description": "Optional human description of what this script does."},
            },
            "required": ["spreadsheet_id", "script_id"],
        },
    ),
    _tool(
        "apps_script_api_list_bound_scripts",
        apps_script_api.list_bound_scripts,
        "apps_script.edit",
        "List all spreadsheet→script mappings the agent has learned. Use to check if a spreadsheet is already registered before asking the user for the script_id.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "apps_script_api_resolve_bound_script",
        apps_script_api.resolve_bound_script,
        "apps_script.edit",
        "Resolve `spreadsheet_id` → its bound Apps Script ID. Tries: local registry → Drive enum → Playwright browser (Extensions→Apps Script). Successful discoveries cached. Returns {script_id, source, account}.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "use_browser": {"type": "boolean", "default": True, "description": "Whether to fall back to Playwright if registry/enumeration fail. Disable for tests."},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    # --- Browser automation (Playwright) ---
    _tool(
        "browser_get_bound_script_id",
        browser.get_bound_script_id,
        "apps_script.edit",
        "Open a sheet in Chromium, click Extensions → Apps Script, capture the bound script_id from the new tab's URL. Only reliable way — APIs won't enumerate bound scripts. First call needs `headless=False` for Google login (profile cached in `.data/browser_profile/`). Usually called via apps_script_api_resolve_bound_script which caches results.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "headless": {"type": "boolean", "default": True, "description": "False shows a visible browser window — needed for first-time login. True runs invisibly after the profile is logged in."},
                "timeout_sec": {"type": "integer", "default": 120},
            },
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "browser_click_custom_menu",
        browser.click_custom_menu,
        "apps_script.run",
        "Open sheet in browser, click through a custom menu chain (e.g. ['☰ WB', 'API', 'Фин.отчеты']) to trigger a bound-script function. Use when scripts.run fails with 403/404 (bound script in Google's default GCP project). Snapshot affected range before/after to verify.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "menu_path": {"type": "array", "items": {"type": "string"}, "description": "Visible text of each menu item top-down. Substring matches OK (e.g. 'WB' matches '☰ WB')."},
                "headless": {"type": "boolean", "default": True},
                "wait_after_click_sec": {"type": "integer", "default": 0, "description": "Hold the browser tab open this many seconds after the final click so the function can finish (max Apps Script runtime is 6 min)."},
                "timeout_sec": {"type": "integer", "default": 120},
            },
            "required": ["spreadsheet_id", "menu_path"],
        },
    ),
    _tool(
        "browser_login_interactive",
        browser.login_interactive,
        "apps_script.edit",
        "Open visible Chromium at Google login. User logs in once; profile cached in `.data/browser_profile/`. Run BEFORE browser_get_bound_script_id with headless=True, or whenever session expires.",
        {
            "type": "object",
            "properties": {
                "timeout_sec": {"type": "integer", "default": 300},
            },
        },
    ),
    # --- Self-healing: agent edits its own source code ---
    _tool(
        "self_read_source",
        self_heal.self_read_source,
        "self.read",
        "Read a source file from this project (under `src/` or `static/`). Returns {path, content, lines, bytes}. Use as the first step when fixing a bug in the agent itself or its UI.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    _tool(
        "self_edit_source",
        self_heal.self_edit_source,
        "self.edit",
        "Replace the contents of a source file (under `src/` or `static/`). USER APPROVAL REQUIRED. After editing, ALWAYS call self_smoke_test to verify the change still imports cleanly. The running process keeps old code in memory until the user restarts the app.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_content": {"type": "string", "description": "Full file content. Make sure to preserve existing code outside the area you're fixing."},
            },
            "required": ["path", "new_content"],
        },
    ),
    _tool(
        "self_smoke_test",
        self_heal.self_smoke_test,
        "self.test",
        "Spawn a fresh Python and verify `src.app` imports cleanly. Returns {ok, exit_code, stdout, stderr}. ALWAYS run after self_edit_source — catches syntax errors and missing imports.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "self_git_diff",
        self_heal.self_git_diff,
        "self.diff",
        "Show pending changes vs HEAD. `staged=True` shows the index; default shows the working tree. `path` narrows to one file. Returns {diff, files_changed, truncated}.",
        {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "default": False},
                "path": {"type": "string"},
            },
        },
    ),
    _tool(
        "self_git_status",
        self_heal.self_git_status,
        "self.diff",
        "`git status --short` — list modified / untracked files. Cheap, no approval.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "self_git_commit",
        self_heal.self_git_commit,
        "self.commit",
        "Stage given `paths` (or all changed tracked files if omitted) and commit with `message`. USER APPROVAL REQUIRED. Adds a 'Co-Authored-By: Claude (self-healing)' line automatically.",
        {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["message"],
        },
    ),
    _tool(
        "self_git_revert",
        self_heal.self_git_revert,
        "self.revert",
        "Discard unstaged changes to `path` (git checkout HEAD -- path). USER APPROVAL REQUIRED. Use when self_smoke_test failed after an edit.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    _tool(
        "apps_script_api_find_bound_script",
        apps_script_api.find_bound_script,
        "apps_script.edit",
        "Brute-force find bound script(s) for a spreadsheet — Drive search by mime='script' misses bound scripts. Enumerates every visible script, calls projects.get, filters by parentId. Slow (~1s/script). Returns [{script_id, title}].",
        {
            "type": "object",
            "properties": {"spreadsheet_id": {"type": "string"}},
            "required": ["spreadsheet_id"],
        },
    ),
    _tool(
        "apps_script_api_edit_file",
        apps_script_api.edit_file,
        "apps_script.edit",
        "Replace ONE file's WHOLE source (add if missing), preserving other files. For surgical fixes to ONE function in a multi-function file prefer apps_script_api_replace_function — safer.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "file_name": {"type": "string", "description": "Without extension. E.g. '2.3 Финансы с датой отчета' (not '...js')."},
                "new_source": {"type": "string"},
                "file_type": {"type": "string", "description": "SERVER_JS (default) / JSON / HTML."},
            },
            "required": ["script_id", "file_name", "new_source"],
        },
    ),
    _tool(
        "apps_script_api_replace_function",
        apps_script_api.replace_function,
        "apps_script.edit",
        "Surgical: replace EXACTLY one function, preserving everything else (comments, whitespace, other functions). Walks JS braces to find span. Prefer over edit_file in multi-function files — eliminates the risk of dropping other functions when the source was truncated.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "file_name": {"type": "string"},
                "function_name": {"type": "string"},
                "new_source": {"type": "string", "description": "Full text of the new function, starting with 'function NAME(...)' and ending with the closing '}'."},
            },
            "required": ["script_id", "file_name", "function_name", "new_source"],
        },
    ),
    _tool(
        "apps_script_api_update_content",
        apps_script_api.update_content,
        "apps_script.edit",
        "Replace the FULL file set of an Apps Script project. Prefer apps_script_api_edit_file for single-file fixes; use this only when modifying multiple files at once. `files` is the complete list of {name, type, source}; any file you omit will be DELETED from the project.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "files": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["script_id", "files"],
        },
    ),
    _tool(
        "apps_script_api_create_version",
        apps_script_api.create_version,
        "apps_script.edit",
        "Create a new VERSION of an Apps Script project — required for libraries: consumer scripts pin a versionNumber, code changes only become visible to them after a new version is created. Returns {scriptId, versionNumber, createTime, description}.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "description": {"type": "string", "description": "Free-text changelog for this version, shown in the script editor's Version manager."},
            },
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_list_versions",
        apps_script_api.list_versions,
        "apps_script.edit",
        "List all versions of an Apps Script project. Useful before creating a new version (to know the next number) or to diagnose 'which version does the consumer pin?'.",
        {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_update_library_dependency",
        apps_script_api.update_library_dependency,
        "apps_script.edit",
        "In consumer `consumer_script_id`, point a library dependency at `new_version`. Adds the entry if missing. Final step of library deploy: (1) edit library file → (2) create_version → (3) update_library_dependency on each consumer → (4) consumer's next call sees fixed code.",
        {
            "type": "object",
            "properties": {
                "consumer_script_id": {"type": "string"},
                "library_script_id": {"type": "string"},
                "new_version": {"type": "integer"},
                "user_symbol": {"type": "string", "description": "Optional. The alias the library is exposed as (e.g. 'Mylib'). Leave empty to preserve existing."},
            },
            "required": ["consumer_script_id", "library_script_id", "new_version"],
        },
    ),
    _tool(
        "apps_script_api_get_project",
        apps_script_api.get_project,
        "apps_script.edit",
        "Project metadata: title, parentId (spreadsheet for bound scripts), owner, createTime.",
        {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_create_project",
        apps_script_api.create_project,
        "apps_script.edit",
        "Create a fresh standalone Apps Script project owned by `account`. Returns {scriptId, title, ...}. Use this for ad-hoc test/runner scripts — then push files via apps_script_api_edit_file. Set parent_id to bind the script to a Drive folder/spreadsheet.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "parent_id": {"type": "string", "description": "Optional Drive ID — if set, script is bound to it (e.g. a spreadsheet)."},
            },
            "required": ["title"],
        },
    ),
    _tool(
        "apps_script_api_create_deployment",
        apps_script_api.create_deployment,
        "apps_script.edit",
        "Create an API-executable deployment of the script pinned to a version_number. Needed for apps_script_api_run_function with dev_mode=False (pinned code). For testing latest code, use dev_mode=True and skip deployment entirely.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "version_number": {"type": "integer"},
                "description": {"type": "string", "default": "API exec"},
            },
            "required": ["script_id", "version_number"],
        },
    ),
    _tool(
        "apps_script_api_run_ad_hoc",
        apps_script_api.run_ad_hoc,
        "apps_script.run",
        "ONE-SHOT: create temp script, push code, run, return result, delete. Best for ad-hoc 'what does this return'. Manifest auto-built with executionApi.access=MYSELF. If library_id+library_version set, library wired as `library_symbol` (default 'Mylib'). keep_project=True retains for inspection.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Full Apps Script source — must define `function_name`."},
                "function_name": {"type": "string", "default": "main"},
                "params": {"type": "array", "description": "Positional args passed to the function."},
                "library_id": {"type": "string", "description": "Optional. Apps Script library script ID to attach as dependency."},
                "library_version": {"type": "integer", "description": "Library version pinned in manifest."},
                "library_symbol": {"type": "string", "default": "Mylib", "description": "Symbol the library is exposed as inside the script."},
                "keep_project": {"type": "boolean", "default": False},
            },
            "required": ["code"],
        },
    ),
    _tool(
        "apps_script_api_run_function",
        apps_script_api.run_function,
        "apps_script.run",
        "Run a function via Apps Script API. Returns {ok, result | error_type/message/stack}. Script's appsscript.json needs `executionApi.access` (\"MYSELF\"). Args via `params` (JSON-serializable list). dev_mode=True runs HEAD (testing); False runs the pinned API-exec deployment.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "function_name": {"type": "string"},
                "params": {"type": "array", "description": "Positional arguments. JSON-serializable values. Empty/omitted for no-arg functions."},
                "dev_mode": {"type": "boolean", "default": True, "description": "True = run HEAD code, False = run pinned API-exec deployment."},
            },
            "required": ["script_id", "function_name"],
        },
    ),
    _tool(
        "apps_script_api_status",
        apps_script_api.status,
        "apps_script.read",
        "Health check for the Apps Script API on `account`. Verifies (a) OAuth token has script.projects/deployments/scriptapp scopes and (b) the API is reachable (via projects.get on `script_id`, or the Phase 14 aggregator if configured). Use BEFORE apps_script_api_run_ad_hoc when in doubt — saves a wasted project-create on a doomed call. Returns {ok, scopes:{required,granted,missing}, api_reachable, api_error?, api_meta?, aggregator?}.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string", "description": "Optional script ID to ping with projects.get. If omitted, falls back to the Phase 14 aggregator; if neither, returns scope-only (api_reachable=null)."},
            },
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
    # NOTE: apps_script_list_files / read_file / write_file (clasp-based) were
    # removed in Phase 12D — use apps_script_api_list_files / api_get_file /
    # api_edit_file instead (multi-account, no clasp login required).
    _tool(
        "apps_script_push",
        apps_script.push,
        "apps_script.edit",
        "Push local clasp-cloned project changes to Google. Only needed when you've used apps_script_clone for legacy clasp-based projects; for normal edits prefer apps_script_api_edit_file (pushes directly).",
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
        "Read UTF-8 text file. Returns {content, total_lines, offset, returned_lines, has_more}. Chunked via offset+limit (0-indexed line offsets). Loop with offset=next_offset until has_more=False for >12k-char files.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "0-based line offset, default 0."},
                "limit": {"type": "integer", "description": "Max lines to return. Omit for whole file (still subject to ~12k-char tool cap — use chunks for big files)."},
            },
            "required": ["path"],
        },
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
        "List entries in a local directory (shallow).",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    _tool(
        "local_walk_dir",
        local_fs.walk_dir,
        "local.read",
        "Recursively list ALL files in a directory. Returns [{rel_path, size, suffix}]. Cuts off at max_files (default 500) so large repos don't blow up. Use when the user attaches a folder and you need to see what's inside.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_files": {"type": "integer", "default": 500},
                "include_hidden": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    ),
    _tool(
        "local_extract_pdf_text",
        local_fs.extract_pdf_text,
        "local.read",
        "Extract text from a PDF (pdfplumber). Returns {file_name, pages_count, text, chars, truncated}. `pages='1-3'`/`'5'` limits range. For BANK STATEMENTS prefer bank_parse_statement.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pages": {"type": "string", "description": "Page range, e.g. '1-3' or '5' or '1,3,5'. Omit for all pages."},
                "max_chars": {"type": "integer", "description": "Cap output (the tool wrapper truncates at 12k anyway)."},
            },
            "required": ["path"],
        },
    ),
    _tool(
        "local_image_info",
        local_fs.image_info,
        "local.read",
        "Get image metadata + a base64 data-URL preview suitable for sending to a multimodal model. Image is downscaled to max 1568px side. Returns {file_name, format, width, height, bytes, data_url}. Use to inspect screenshots, photos, or other images the user attaches.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    # --- Bank statement parser (PDFs of Russian banks) ---
    _tool(
        "bank_parse_statement",
        bank_parser.parse_bank_statement,
        "local.read",
        "Parse a Russian bank statement PDF or 1С client-bank .txt. Auto-detects: Сбер, Альфа, Т-Банк, Газпром, ВТБ, Райф, ЮниКредит, Ozon, Modul, Точка, WB, 1С. Returns {bank, transactions: [{date, description, amount_cents, inn?, counterparty?}], account_last4?}. **amount_cents in КОПЕЙКАХ** (×0.01 для ₽).",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "bank_hint": {"type": "string", "description": "Optional. One of: alfa, sber, sber_business, tinkoff, gazprom, vtb, raif, unicredit, ozon, modul, tochka, wb_bank, clientbank_1c."},
            },
            "required": ["file_path"],
        },
    ),
    _tool(
        "bank_detect",
        bank_parser.detect_bank,
        "local.read",
        "Quick check whether a file is a recognized bank statement. Returns {bank} (e.g. 'sber') or {bank: null, error: 'no parser matched'}. Cheap — runs each parser's can_parse() in order. Use before parse_statement to confirm format.",
        {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
    ),
    _tool(
        "bank_list_supported",
        bank_parser.list_supported_banks,
        "local.read",
        "List the bank ids the parser supports.",
        {"type": "object", "properties": {}},
    ),
    # --- Analytics (ABC) ---
    _tool(
        "analytics_abc",
        analytics.abc_analysis,
        "local.read",
        "ABC analysis (80/15/5) on row dicts. Groups by sku_col, sums revenue/qty/profit, computes ABC class per metric, composite code ('AAA'=leader, 'CCC'=cut). Returns {total_skus, total_revenue, categories, abc_rev_counts, top_a, rows}. Optional `costs`=[{sku, cost}] → final_profit = revenue − cost×qty.",
        {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "description": "List of row dicts (e.g. from sheets_query or excel_parse). Must have sku, revenue, qty columns."},
                "sku_col": {"type": "string", "default": "sku"},
                "revenue_col": {"type": "string", "default": "revenue"},
                "qty_col": {"type": "string", "default": "qty"},
                "profit_col": {"type": "string", "default": "profit"},
                "costs": {"type": "array", "description": "Optional [{sku, cost}] — purchase cost per SKU for final_profit calculation."},
            },
            "required": ["rows"],
        },
    ),
    _tool(
        "analytics_abc_split",
        analytics.abc_split,
        "local.read",
        "Quick 1-metric ABC classification on rows. Sorts rows by `metric` desc, cumsum, assigns A (≤80%), B (≤95%), C (rest). Returns rows with new `abc` key. Use when you only need ABC on ONE metric (vs analytics_abc which does 3-metric composite).",
        {
            "type": "object",
            "properties": {
                "rows": {"type": "array"},
                "metric": {"type": "string"},
            },
            "required": ["rows", "metric"],
        },
    ),
    # --- Report storage (typed memory for structured data) ---
    _tool(
        "report_save",
        reports.save_report,
        "notes.write",
        "Save structured data (rows/stats/parsed) to `.data/reports/<kind>/<name>.json`. `kind` namespaces (e.g. 'bank', 'abc', 'sales'). For typed data the agent loads back later. E.g. after bank_parse_statement: save_report(name='varychev_alfa_dec_2025', kind='bank', data=transactions).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique within the kind. Use kebab/snake case."},
                "kind": {"type": "string", "description": "Namespace: 'bank', 'abc', 'sales', 'expenses', etc."},
                "data": {"description": "JSON-serializable data — list of dicts or a dict."},
                "metadata": {"type": "object", "description": "Optional context: source file, date range, account, etc."},
            },
            "required": ["name", "kind", "data"],
        },
    ),
    _tool(
        "report_load",
        reports.load_report,
        "notes.read",
        "Load a saved report by name. Returns the full payload {name, kind, saved_at, metadata, data}. Pass `kind` to disambiguate if the same name exists in multiple kinds.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    _tool(
        "report_list",
        reports.list_reports,
        "notes.read",
        "List all saved reports. Filter by kind if given. Returns [{name, kind, saved_at, bytes, metadata_keys}], newest first.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    _tool(
        "report_delete",
        reports.delete_report,
        "notes.write",
        "Delete a saved report by name (optionally within a kind).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    # --- Watcher (Apps Script failure monitoring via Cloud Logging) ---
    _tool(
        "watcher_recent_failures",
        watcher.recent_failures,
        "apps_script.edit",
        "Recent Apps Script failures for `script_id` via Cloud Logging (Exception, SyntaxError, 429, etc). Returns {failures: [{timestamp, function, execution_id, severity, kind, message}]}. Use for 'проверь не падал ли X' or after deploy.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "since_minutes": {"type": "integer", "default": 60},
            },
            "required": ["script_id"],
        },
    ),
    _tool(
        "watcher_poll_known_scripts",
        watcher.poll_known_scripts,
        "apps_script.edit",
        "Scan ALL monitored scripts (Mylib + everything in the bound-script registry) for failures and append new ones to the alerts queue. Background watcher runs this every 5 min automatically; call manually to force an immediate check. Idempotent (won't duplicate).",
        {
            "type": "object",
            "properties": {"since_minutes": {"type": "integer", "default": 30}},
        },
    ),
    _tool(
        "watcher_list_alerts",
        watcher.list_alerts,
        "notes.read",
        "List queued failure alerts (newest first). Pass unread_only=True to see only fresh ones. Each alert: {id, script_label, function, kind, timestamp, message, read}.",
        {
            "type": "object",
            "properties": {
                "unread_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    _tool(
        "watcher_mark_alerts_read",
        watcher.mark_alerts_read,
        "notes.write",
        "Mark alerts as read. If alert_ids is omitted, marks ALL as read.",
        {
            "type": "object",
            "properties": {
                "alert_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    _tool(
        "report_combine",
        reports.combine_reports,
        "notes.write",
        "Merge saved reports into one row set keyed by `merge_key`; `sum_cols` summed across rows with same key. Optional `save_as` persists. Use for: monthly bank → yearly; per-store sales → company-wide. Returns {merged_count, sources, rows}.",
        {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}, "description": "Saved-report names to merge."},
                "merge_key": {"type": "string", "description": "Column name to group by (e.g. 'sku', 'counterparty', 'date')."},
                "sum_cols": {"type": "array", "items": {"type": "string"}, "description": "Numeric columns to sum across reports."},
                "keep_first_cols": {"type": "array", "items": {"type": "string"}, "description": "Columns to keep value from first occurrence (default: all non-numeric)."},
                "kind": {"type": "string", "description": "Optional: restrict source lookups to this kind."},
                "save_as": {"type": "string", "description": "Optional name to save the merged result as a new report (kind='combined')."},
            },
            "required": ["names", "merge_key"],
        },
    ),
    # --- Google Calendar ---
    _tool(
        "calendar_list_calendars",
        calendar.list_calendars,
        "calendar.read",
        "List all calendars the account has access to. Identifies the 'primary' one.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "calendar_list_events",
        calendar.list_events,
        "calendar.read",
        "Events in a date range. time_min/time_max: 'YYYY-MM-DD' or RFC3339. If both omitted → defaults to today+7d, flagged via `_meta.window.default_used=true`. Returns {events, _meta:{window, truncated}}. ALWAYS surface the scanned window before saying 'нет встреч' / 'свободно'.",
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "Default: now. Format 'YYYY-MM-DD' or RFC3339."},
                "time_max": {"type": "string", "description": "Default: +7 days."},
                "calendar_id": {"type": "string", "description": "Default 'primary'."},
                "max_results": {"type": "integer", "description": "Default 50, max 250."},
                "query": {"type": "string", "description": "Optional free-text filter."},
            },
        },
    ),
    _tool(
        "calendar_get_event",
        calendar.get_event,
        "calendar.read",
        "Full details of one event by id.",
        {"type": "object", "properties": {"event_id": {"type": "string"}, "calendar_id": {"type": "string"}}, "required": ["event_id"]},
    ),
    _tool(
        "calendar_create_event",
        calendar.create_event,
        "calendar.write",
        "Create event. start/end: 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed in `timezone_str`); end defaults to start+1h. reminder_minutes popup, None=no reminder. `recurrence` accepts RFC5545 RRULEs e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10'].",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string", "description": "'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed)."},
                "end": {"type": "string", "description": "Optional. Same format as start."},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "List of email addresses."},
                "calendar_id": {"type": "string", "description": "Default 'primary'."},
                "reminder_minutes": {"type": "integer", "description": "Minutes before event to popup (default 15). 0 = at start. null = no reminder."},
                "timezone_str": {"type": "string", "description": "Default 'Europe/Moscow'."},
                "recurrence": {"type": "array", "items": {"type": "string"}, "description": "RFC5545 RRULE strings for repeating events."},
            },
            "required": ["summary", "start"],
        },
    ),
    _tool(
        "calendar_update_event",
        calendar.update_event,
        "calendar.write",
        "Patch fields on an existing event. `updates` is a dict with any of: summary, description, location, start, end, attendees, reminders, status. For time fields use {date} or {dateTime, timeZone}.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "updates": {"type": "object"},
                "calendar_id": {"type": "string"},
            },
            "required": ["event_id", "updates"],
        },
    ),
    _tool(
        "calendar_delete_event",
        calendar.delete_event,
        "calendar.delete",
        "Delete an event.",
        {"type": "object", "properties": {"event_id": {"type": "string"}, "calendar_id": {"type": "string"}}, "required": ["event_id"]},
    ),
    _tool(
        "calendar_find_free_time",
        calendar.find_free_time,
        "calendar.read",
        "Find free slots of `duration_minutes` between work_hours_start..work_hours_end across a date range. Uses Calendar's free/busy. Returns up to 20 earliest slots. Use for 'когда у меня свободно' / 'найди время на встречу с Х'.",
        {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "'YYYY-MM-DD'."},
                "end_date": {"type": "string", "description": "'YYYY-MM-DD' (inclusive)."},
                "duration_minutes": {"type": "integer", "description": "Default 60."},
                "work_hours_start": {"type": "integer", "description": "Default 9."},
                "work_hours_end": {"type": "integer", "description": "Default 19."},
                "weekdays_only": {"type": "boolean", "description": "Default true (skip Sat/Sun)."},
                "calendar_id": {"type": "string"},
                "timezone_str": {"type": "string"},
            },
            "required": ["start_date", "end_date"],
        },
    ),
    _tool(
        "calendar_quick_reminder",
        calendar.quick_reminder,
        "calendar.write",
        "Shortcut for 'напомни мне когда': creates a brief event at `when` with a popup reminder. Use for simple reminders like 'напомни мне в среду в 15:00 проверить ВБ-отчёт'. reminder_minutes=0 → popup at event start.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind about (becomes event title)."},
                "when": {"type": "string", "description": "'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'."},
                "reminder_minutes": {"type": "integer", "description": "Default 0 = popup at event start."},
            },
            "required": ["text", "when"],
        },
    ),
    # --- Phase 6: Calendar group ops ---
    _tool(
        "calendar_freebusy",
        calendar.freebusy,
        "calendar.read",
        "Query free/busy slots across one or more calendars (by email). Returns {per_email: [{email, busy: [{start, end}], errors: []}], _meta:{time_min, time_max}}. Use for 'когда у X занято на этой неделе'.",
        {
            "type": "object",
            "properties": {
                "emails": {"type": "array", "items": {"type": "string"}},
                "time_min": {"type": "string", "description": "YYYY-MM-DD or RFC3339."},
                "time_max": {"type": "string"},
            },
            "required": ["emails", "time_min", "time_max"],
        },
    ),
    _tool(
        "calendar_find_meeting_slot",
        calendar.find_meeting_slot,
        "calendar.read",
        "Find the FIRST common free slot of `duration_minutes` for all `attendees` in [time_min, time_max]. Defaults to weekdays 09:00-19:00 in the calendar's local time. Returns {found, slot, candidates_checked}.",
        {
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"}},
                "duration_minutes": {"type": "integer"},
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "working_hours_only": {"type": "boolean"},
                "work_hours_start": {"type": "integer"},
                "work_hours_end": {"type": "integer"},
                "weekdays_only": {"type": "boolean"},
            },
            "required": ["attendees", "duration_minutes", "time_min", "time_max"],
        },
    ),
    _tool(
        "calendar_list_recurring_instances",
        calendar.list_recurring_instances,
        "calendar.read",
        "Expand a recurring event into its concrete instances within a window. Use after creating a recurring event to confirm when its repetitions fall.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "calendar_id": {"type": "string"},
            },
            "required": ["event_id", "time_min", "time_max"],
        },
    ),
    _tool(
        "calendar_overlay_accounts",
        calendar.overlay_accounts,
        "calendar.read",
        "Cross-account FreeBusy: pass {account_alias: [emails]} and get a unified busy/free map across multiple configured Google accounts. Useful when consolidating availability across the user's personal + work accounts.",
        {
            "type": "object",
            "properties": {
                "emails_per_account": {"type": "object"},
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
            },
            "required": ["emails_per_account", "time_min", "time_max"],
        },
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
    _tool(
        "auth_describe_account",
        auth.describe_account,
        "auth.list",
        "Identify which Google account is bound to a token alias. Returns {email, name, scopes}. Use after auth_add_account to verify the consent screen picked the right account (we've been burned by accidentally picking the wrong one).",
        {"type": "object", "properties": {"account": {"type": "string", "default": "main"}}},
    ),
    _tool(
        "auth_list_accounts_with_identity",
        auth.list_accounts_with_identity,
        "auth.list",
        "Like auth_list_accounts but also fetches each alias's bound email + name. One-stop 'who is what'.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "auth_add_account_incremental",
        auth.add_account_incremental,
        "auth.add",
        "Re-authorize an account adding NEW scopes while preserving existing grants (Google's incremental authorization with include_granted_scopes=true). Cleaner than delete+re-add — the user only sees the new scopes in the consent screen.",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "new_scopes": {"type": "array", "items": {"type": "string"}, "description": "Scope URLs to add."},
            },
            "required": ["account"],
        },
    ),
    # --- WB (Wildberries) direct API ---
    _tool(
        "wb_check_token",
        wb.check_token,
        "drive.read",  # No external write — read-only ping of WB API families
        "Ping every Wildberries API family (content/analytics/statistics/advert/marketplace/common) with `token`. Returns {family: {code, status}}. Use to verify a token has the expected access scopes BEFORE running a long fetch.",
        {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
    ),
    _tool(
        "wb_token_age",
        wb.token_age,
        "drive.read",
        "Decode a WB JWT (no signature verification, just claims) and return issued_at/expires_at/days_left/seller_id. Use to warn the user when a token is close to expiry.",
        {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
    ),
    _tool(
        "wb_finance_detail_collect",
        wb.finance_detail_collect,
        "drive.read",
        "Fetch WB reportDetailByPeriod for [date_from..date_to]. Paginated by rrd_id, 65s pause between pages (WB rate-limit 1 req/min), honors X-Ratelimit-Retry. Returns {rows_count, last_rrd_id, pages, sample_first, sample_last}. `response_format='concise'` (default) = sample only; 'detailed' adds full `rows` (can be tens of MB). Will raise immediately if recent run consumed budget — 12h ban risk.",
        {
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, defaults to today UTC"},
                "limit": {"type": "integer", "default": 10000},
                "start_rrd_id": {"type": "integer", "default": 0},
                "sleep_sec": {"type": "integer", "default": 65, "description": "Pause between pages."},
                "max_pages": {"type": "integer", "description": "Stop after N pages — useful for testing."},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["token", "date_from"],
        },
    ),
    # --- WB marketplace read-only extensions (Batch 1: 10 tools) ---
    _tool(
        "wb_stocks_v2",
        wb.stocks_v2,
        "drive.read",
        "WB FBO stocks snapshot via `/api/v1/supplier/stocks`. Returns full array (no pagination — WB returns one big response, up to ~50MB). Each row: {barcode, brand, category, lastChangeDate, quantity, quantityFull, Price, Discount, ...}.",
        {"type": "object", "properties": {"token": {"type": "string"}, "date_from": {"type": "string", "description": "YYYY-MM-DD; defaults to today UTC."}}, "required": ["token"]},
    ),
    _tool(
        "wb_orders_recent",
        wb.orders_recent,
        "drive.read",
        "Recent FBO+FBS orders since `date_from`. `flag=0` (default): delta since last call; `flag=1`: full window. Returns {ok, data: [{date, lastChangeDate, supplierArticle, nmId, ...}], _meta:{ratelimit}}.",
        {"type": "object", "properties": {"token": {"type": "string"}, "date_from": {"type": "string", "description": "YYYY-MM-DD"}, "flag": {"type": "integer", "default": 0, "description": "0=delta since last call, 1=full window."}}, "required": ["token", "date_from"]},
    ),
    _tool(
        "wb_sales_recent",
        wb.sales_recent,
        "drive.read",
        "Recent sales+returns since `date_from`. saleID prefix: S=sale, R=return. Same flag semantics as wb_orders_recent.",
        {"type": "object", "properties": {"token": {"type": "string"}, "date_from": {"type": "string"}, "flag": {"type": "integer", "default": 0}}, "required": ["token", "date_from"]},
    ),
    _tool(
        "wb_warehouses",
        wb.warehouses,
        "drive.read",
        "Official WB warehouse list (IDs you use in the marketplace API for FBS supplies). Returns [{id, name, address, ...}].",
        {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
    ),
    _tool(
        "wb_prices_list",
        wb.prices_list,
        "drive.read",
        "Current seller prices + discounts via `/api/v2/list/goods/filter`. Paginated. Returns {listGoods: [{nmID, vendorCode, sizes:[{discountedPrice, price}], ...}]}.",
        {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000, "description": "Max 1000."}, "offset": {"type": "integer", "default": 0}}, "required": ["token"]},
    ),
    _tool(
        "wb_questions_count",
        wb.questions_count,
        "drive.read",
        "Count of buyer questions (FBO/FBS). Use `is_answered=false` for backlog. Quick SLA-monitoring tool.",
        {"type": "object", "properties": {"token": {"type": "string"}, "is_answered": {"type": "boolean"}}, "required": ["token"]},
    ),
    _tool(
        "wb_questions_list",
        wb.questions_list,
        "drive.read",
        "List buyer questions. `date_from`/`date_to` are UNIX timestamps (seconds). Returns {data:{questions:[{id, text, nmId, productDetails, ...}]}}.",
        {"type": "object", "properties": {"token": {"type": "string"}, "take": {"type": "integer", "default": 100}, "skip": {"type": "integer", "default": 0}, "is_answered": {"type": "boolean"}, "date_from": {"type": "integer"}, "date_to": {"type": "integer"}}, "required": ["token"]},
    ),
    _tool(
        "wb_feedbacks_count",
        wb.feedbacks_count,
        "drive.read",
        "Count of customer reviews (отзывы).",
        {"type": "object", "properties": {"token": {"type": "string"}, "is_answered": {"type": "boolean"}}, "required": ["token"]},
    ),
    _tool(
        "wb_feedbacks_list",
        wb.feedbacks_list,
        "drive.read",
        "List customer reviews. `order`: dateDesc | dateAsc. Returns {data:{feedbacks:[{id, productValuation, text, ...}]}}.",
        {"type": "object", "properties": {"token": {"type": "string"}, "take": {"type": "integer", "default": 100}, "skip": {"type": "integer", "default": 0}, "is_answered": {"type": "boolean"}, "order": {"type": "string", "enum": ["dateDesc", "dateAsc"], "default": "dateDesc"}}, "required": ["token"]},
    ),
    _tool(
        "wb_supplies_list",
        wb.supplies_list,
        "drive.read",
        "FBS supplies (заказы на отгрузку). `next_id` for pagination cursor.",
        {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "next_id": {"type": "integer", "default": 0}}, "required": ["token"]},
    ),
    _tool(
        "wb_adverts_count",
        wb.adverts_list,
        "drive.read",
        "Count of advertising campaigns. `status`: -1=pause,4=ready,7=done,8=draft,9=active. `type_`: 4=catalog,5=cards,6=search,7=recommendation,8=auto,9=search-catalog.",
        {"type": "object", "properties": {"token": {"type": "string"}, "status": {"type": "integer"}, "type_": {"type": "integer"}}, "required": ["token"]},
    ),
    _tool(
        "wb_analytics_paid_storage",
        wb.analytics_paid_storage,
        "drive.read",
        "Paid-storage cost report (FBO) for [date_from..date_to]. Use for unit-economy: what each SKU costs in warehouse fees.",
        {"type": "object", "properties": {"token": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}}, "required": ["token", "date_from", "date_to"]},
    ),
    # --- Ozon Seller API (Batch 2: 12 tools) ---
    _tool(
        "ozon_check_credentials",
        ozon.check_credentials,
        "drive.read",
        "Cheapest call to verify Ozon (Client-Id, Api-Key) pair. Returns {ok, credentials_valid, _meta:{http_status, ratelimit}}. Call BEFORE batch fetches.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_stocks_fbo",
        ozon.stocks_fbo,
        "drive.read",
        "FBO stocks via /v4/product/info/stocks. Paginated by cursor — pass `last_id` from previous response.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "cursor": {"type": "string", "default": ""}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_stocks_fbs",
        ozon.stocks_fbs,
        "drive.read",
        "FBS stocks for specific SKUs via /v1/product/info/stocks-by-warehouse/fbs.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "sku": {"type": "array", "items": {"type": "string"}}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_orders_fbo_list",
        ozon.orders_fbo_list,
        "drive.read",
        "FBO postings via /v2/posting/fbo/list. Dates RFC3339 (`2026-05-01T00:00:00Z`). `response_format='concise'` (default) skips analytics_data+financial_data fields to save ~70% tokens; 'detailed' includes them.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}, "required": ["client_id", "api_key", "date_from", "date_to"]},
    ),
    _tool(
        "ozon_orders_fbs_list",
        ozon.orders_fbs_list,
        "drive.read",
        "FBS postings via /v3/posting/fbs/list. Optional `status`: awaiting_packaging / awaiting_deliver / delivered / cancelled. `response_format='concise'` skips analytics+financial fields.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "status": {"type": "string"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}, "required": ["client_id", "api_key", "date_from", "date_to"]},
    ),
    _tool(
        "ozon_returns_list",
        ozon.returns_list,
        "drive.read",
        "Returns (возвраты) via /v1/returns/company/fbo. Dates RFC3339.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}}, "required": ["client_id", "api_key", "date_from", "date_to"]},
    ),
    _tool(
        "ozon_finance_realization",
        ozon.finance_realization,
        "drive.read",
        "Monthly realization report (отчёт о реализации) via /v2/finance/realization.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "year": {"type": "integer"}, "month": {"type": "integer"}}, "required": ["client_id", "api_key", "year", "month"]},
    ),
    _tool(
        "ozon_finance_transactions",
        ozon.finance_transactions,
        "drive.read",
        "Detailed transactions via /v3/finance/transaction/list. Dates RFC3339. Pass `operation_type` array to filter (e.g. ['OperationAgentDeliveredToCustomer']).",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "page": {"type": "integer", "default": 1}, "page_size": {"type": "integer", "default": 1000}, "operation_type": {"type": "array", "items": {"type": "string"}}}, "required": ["client_id", "api_key", "date_from", "date_to"]},
    ),
    _tool(
        "ozon_products_list",
        ozon.products_list,
        "drive.read",
        "Product list via /v3/product/list. visibility: ALL, VISIBLE, INVISIBLE, EMPTY_STOCK, NOT_MODERATED, ARCHIVED. Paginated by `last_id`.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "last_id": {"type": "string", "default": ""}, "visibility": {"type": "string", "default": "ALL"}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_prices_list",
        ozon.prices_list,
        "drive.read",
        "Current prices via /v4/product/info/prices. Paginated.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "last_id": {"type": "string", "default": ""}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_warehouses_list",
        ozon.warehouses_list,
        "drive.read",
        "FBS warehouses via /v1/warehouse/list.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}}, "required": ["client_id", "api_key"]},
    ),
    _tool(
        "ozon_analytics_data",
        ozon.analytics_data,
        "drive.read",
        "Daily analytics via /v1/analytics/data. metrics: revenue, ordered_units, delivered_units, returns, cancellations, hits_view_search, hits_view_pdp. dimension: day, week, month, sku, brand, category1-4.",
        {"type": "object", "properties": {"client_id": {"type": "string"}, "api_key": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "metrics": {"type": "array", "items": {"type": "string"}}, "dimension": {"type": "array", "items": {"type": "string"}}}, "required": ["client_id", "api_key", "date_from", "date_to"]},
    ),
    # --- Yandex Market Partner API (Batch 3: 9 tools) ---
    _tool(
        "yamarket_campaigns_list",
        yamarket.campaigns_list,
        "drive.read",
        "List Yandex Market campaigns (shops) the api_key has access to. Returns {data:{campaigns:[{id, domain, business, ...}]}}. The `campaignId` is what every per-shop endpoint takes.",
        {"type": "object", "properties": {"api_key": {"type": "string"}}, "required": ["api_key"]},
    ),
    _tool(
        "yamarket_businesses_list",
        yamarket.businesses_list,
        "drive.read",
        "List Yandex Market businesses (legal entities). `businessId` scopes products/inventory.",
        {"type": "object", "properties": {"api_key": {"type": "string"}}, "required": ["api_key"]},
    ),
    _tool(
        "yamarket_stocks_list",
        yamarket.stocks_list,
        "drive.read",
        "Stocks for a campaign. Paginate via `paging.nextPageToken` returned in the response.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "campaign_id": {"type": "integer"}, "limit": {"type": "integer", "default": 200}, "page_token": {"type": "string", "default": ""}, "with_turnover": {"type": "boolean", "default": False}}, "required": ["api_key", "campaign_id"]},
    ),
    _tool(
        "yamarket_orders_list",
        yamarket.orders_list,
        "drive.read",
        "Orders for a campaign. Dates DD-MM-YYYY (Yandex quirk). status: PROCESSING / DELIVERY / DELIVERED / CANCELLED / PICKUP. `response_format='concise'` trims each order to id+status+date+total.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "campaign_id": {"type": "integer"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}, "page": {"type": "integer", "default": 1}, "page_size": {"type": "integer", "default": 50}, "status": {"type": "string"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}, "required": ["api_key", "campaign_id", "from_date", "to_date"]},
    ),
    _tool(
        "yamarket_order_get",
        yamarket.order_get,
        "drive.read",
        "Single order detail by id.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "campaign_id": {"type": "integer"}, "order_id": {"type": "integer"}}, "required": ["api_key", "campaign_id", "order_id"]},
    ),
    _tool(
        "yamarket_returns_list",
        yamarket.returns_list,
        "drive.read",
        "Returns for a campaign. Dates DD-MM-YYYY.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "campaign_id": {"type": "integer"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}, "page_token": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 50}}, "required": ["api_key", "campaign_id", "from_date", "to_date"]},
    ),
    _tool(
        "yamarket_prices_list",
        yamarket.prices_list,
        "drive.read",
        "Current shop prices.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "campaign_id": {"type": "integer"}, "page_token": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 100}}, "required": ["api_key", "campaign_id"]},
    ),
    _tool(
        "yamarket_offers_list",
        yamarket.offers_list,
        "drive.read",
        "Business-level offer catalog (across all campaigns).",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "business_id": {"type": "integer"}, "page_token": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 200}}, "required": ["api_key", "business_id"]},
    ),
    _tool(
        "yamarket_warehouses_list",
        yamarket.warehouses_list,
        "drive.read",
        "Business warehouses.",
        {"type": "object", "properties": {"api_key": {"type": "string"}, "business_id": {"type": "integer"}}, "required": ["api_key", "business_id"]},
    ),
    # --- СДЭК (Batch 4: 5 tools) ---
    _tool(
        "cdek_auth",
        logistics.cdek_auth,
        "drive.read",
        "Get SDEK OAuth2 access token (lifetime 1h). Pass result.data.access_token as `token` to other cdek_* tools.",
        {"type": "object", "properties": {"account": {"type": "string"}, "secret": {"type": "string"}}, "required": ["account", "secret"]},
    ),
    _tool(
        "cdek_orders_list",
        logistics.cdek_orders_list,
        "drive.read",
        "List SDEK shipments. Dates ISO8601. Paginated by limit+offset.",
        {"type": "object", "properties": {"token": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}, "required": ["token"]},
    ),
    _tool(
        "cdek_order_get",
        logistics.cdek_order_get,
        "drive.read",
        "Single SDEK shipment by UUID.",
        {"type": "object", "properties": {"token": {"type": "string"}, "uuid": {"type": "string"}}, "required": ["token", "uuid"]},
    ),
    _tool(
        "cdek_calculator",
        logistics.cdek_calculator,
        "drive.read",
        "SDEK cost calculator. `from_code`/`to_code` are SDEK location codes (use cdek_locations_search). `tariff_code` 136 = склад-склад.",
        {"type": "object", "properties": {"token": {"type": "string"}, "from_code": {"type": "integer"}, "to_code": {"type": "integer"}, "tariff_code": {"type": "integer", "default": 136}, "weight_g": {"type": "integer", "default": 1000}, "length_cm": {"type": "integer", "default": 10}, "width_cm": {"type": "integer", "default": 10}, "height_cm": {"type": "integer", "default": 10}}, "required": ["token", "from_code", "to_code"]},
    ),
    _tool(
        "cdek_locations_search",
        logistics.cdek_locations_search,
        "drive.read",
        "Search SDEK location codes by city name. Use the returned code in other endpoints.",
        {"type": "object", "properties": {"token": {"type": "string"}, "query": {"type": "string"}, "country_code": {"type": "string", "default": "RU"}, "size": {"type": "integer", "default": 20}}, "required": ["token", "query"]},
    ),
    # --- Boxberry (Batch 5: 6 tools) ---
    _tool(
        "boxberry_list_parcels",
        logistics.boxberry_list_parcels,
        "drive.read",
        "List uploaded Boxberry parcels. `from_id` is the resume-cursor (parcel id).",
        {"type": "object", "properties": {"token": {"type": "string"}, "from_id": {"type": "string", "default": ""}}, "required": ["token"]},
    ),
    _tool(
        "boxberry_parcel_check",
        logistics.boxberry_parcel_check,
        "drive.read",
        "Verify one Boxberry parcel by your internal id.",
        {"type": "object", "properties": {"token": {"type": "string"}, "im_id": {"type": "string"}}, "required": ["token", "im_id"]},
    ),
    _tool(
        "boxberry_list_statuses",
        logistics.boxberry_list_statuses,
        "drive.read",
        "Status history for one Boxberry parcel.",
        {"type": "object", "properties": {"token": {"type": "string"}, "im_id": {"type": "string"}}, "required": ["token", "im_id"]},
    ),
    _tool(
        "boxberry_list_services",
        logistics.boxberry_list_services,
        "drive.read",
        "Cost breakdown (delivery, insurance, ...) for one Boxberry parcel.",
        {"type": "object", "properties": {"token": {"type": "string"}, "im_id": {"type": "string"}}, "required": ["token", "im_id"]},
    ),
    _tool(
        "boxberry_courier_list_cities",
        logistics.boxberry_courier_list_cities,
        "drive.read",
        "Cities where Boxberry courier pickup is available.",
        {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
    ),
    _tool(
        "boxberry_list_points",
        logistics.boxberry_list_points,
        "drive.read",
        "List Boxberry pickup points. `city_code` optional filter.",
        {"type": "object", "properties": {"token": {"type": "string"}, "city_code": {"type": "string", "default": ""}}, "required": ["token"]},
    ),
    # --- Почта России (Batch 6: 5 tools) ---
    _tool(
        "pochta_track",
        logistics.pochta_track,
        "drive.read",
        "Track a single Russian Post barcode. login+password are your tracking.pochta.ru account; we base64-encode them per API spec.",
        {"type": "object", "properties": {"token": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "barcode": {"type": "string"}}, "required": ["token", "login", "password", "barcode"]},
    ),
    _tool(
        "pochta_orders_search",
        logistics.pochta_orders_search,
        "drive.read",
        "Search otpravka-api orders by recipient name / order-num.",
        {"type": "object", "properties": {"token": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "default": 50}}, "required": ["token", "query"]},
    ),
    _tool(
        "pochta_order_get",
        logistics.pochta_order_get,
        "drive.read",
        "Single Pochta order detail by id.",
        {"type": "object", "properties": {"token": {"type": "string"}, "order_id": {"type": "integer"}}, "required": ["token", "order_id"]},
    ),
    _tool(
        "pochta_tariff_calc",
        logistics.pochta_tariff_calc,
        "drive.read",
        "Tariff calculator. mass_g grams, indexes are 6-digit postal codes. mail_type: POSTAL_PARCEL / ONLINE_PARCEL / EMS.",
        {"type": "object", "properties": {"token": {"type": "string"}, "mass_g": {"type": "integer"}, "index_from": {"type": "string"}, "index_to": {"type": "string"}, "mail_category": {"type": "string", "default": "ORDINARY"}, "mail_type": {"type": "string", "default": "POSTAL_PARCEL"}}, "required": ["token", "mass_g", "index_from", "index_to"]},
    ),
    _tool(
        "pochta_normalize_address",
        logistics.pochta_normalize_address,
        "drive.read",
        "Address normalizer — parses a free-form address string into components + delivery-area metadata.",
        {"type": "object", "properties": {"token": {"type": "string"}, "address": {"type": "string"}}, "required": ["token", "address"]},
    ),
    # --- МойСклад (Batch 7: 14 tools) ---
    _tool("moysklad_products_list", moysklad.products_list, "drive.read",
          "МойСклад товары. `filter_str` is the МС filter DSL, e.g. `name~Шланг;archived=false`.",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "filter_str": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_variants_list", moysklad.variants_list, "drive.read",
          "МС модификации (size/color SKU variants).",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}}, "required": ["token"]}),
    _tool("moysklad_services_list", moysklad.services_list, "drive.read",
          "МС услуги.",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}}, "required": ["token"]}),
    _tool("moysklad_counterparties_list", moysklad.counterparties_list, "drive.read",
          "МС контрагенты (customers + suppliers).",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "filter_str": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_stores_list", moysklad.stores_list, "drive.read",
          "МС склады.",
          {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_organizations_list", moysklad.organizations_list, "drive.read",
          "МС юр.лица.",
          {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_customerorders_list", moysklad.customerorders_list, "drive.read",
          "МС заказы покупателей. moment_from/to format `2026-05-01 00:00:00`. `response_format='concise'` trims to id+moment+sum+agent only.",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "moment_from": {"type": "string"}, "moment_to": {"type": "string"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}, "required": ["token"]}),
    _tool("moysklad_demands_list", moysklad.demands_list, "drive.read",
          "МС отгрузки (revenue recognition events).",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "moment_from": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_supplies_list", moysklad.supplies_list, "drive.read",
          "МС приёмки от поставщиков.",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "moment_from": {"type": "string"}}, "required": ["token"]}),
    _tool("moysklad_stock_all", moysklad.stock_all, "drive.read",
          "МС остатки по всем складам.",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}}, "required": ["token"]}),
    _tool("moysklad_stock_bystore", moysklad.stock_bystore, "drive.read",
          "МС остатки в одном складе.",
          {"type": "object", "properties": {"token": {"type": "string"}, "store_id": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}}, "required": ["token", "store_id"]}),
    _tool("moysklad_cashflow_report", moysklad.cashflow_report, "drive.read",
          "МС cashflow — money in/out grouped by day.",
          {"type": "object", "properties": {"token": {"type": "string"}, "moment_from": {"type": "string"}, "moment_to": {"type": "string"}}, "required": ["token", "moment_from", "moment_to"]}),
    _tool("moysklad_profit_byproduct", moysklad.profit_byproduct, "drive.read",
          "МС прибыль по товарам — margin per SKU. Closest-to-truth unit-econ report МС provides.",
          {"type": "object", "properties": {"token": {"type": "string"}, "moment_from": {"type": "string"}, "moment_to": {"type": "string"}, "limit": {"type": "integer", "default": 1000}}, "required": ["token", "moment_from", "moment_to"]}),
    _tool("moysklad_expenses_list", moysklad.expenses_list, "drive.read",
          "МС расходы (cashout).",
          {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer", "default": 1000}, "offset": {"type": "integer", "default": 0}, "moment_from": {"type": "string"}}, "required": ["token"]}),
    # --- SMS gateways + Telegram bot + IMAP (Batch 8: 11 tools) ---
    _tool("smsru_send", messaging.smsru_send, "gmail.send",
          "Send SMS via SMS.ru. `test=1` simulates without spending balance.",
          {"type": "object", "properties": {"api_id": {"type": "string"}, "to": {"type": "string"}, "msg": {"type": "string"}, "from_": {"type": "string"}, "test": {"type": "integer", "default": 0}}, "required": ["api_id", "to", "msg"]}),
    _tool("smsru_balance", messaging.smsru_balance, "drive.read",
          "SMS.ru balance (RUB).",
          {"type": "object", "properties": {"api_id": {"type": "string"}}, "required": ["api_id"]}),
    _tool("smsru_status", messaging.smsru_status, "drive.read",
          "Delivery status of one SMS by sms_id.",
          {"type": "object", "properties": {"api_id": {"type": "string"}, "sms_id": {"type": "string"}}, "required": ["api_id", "sms_id"]}),
    _tool("smsc_send", messaging.smsc_send, "gmail.send",
          "Send SMS via SMSC.ru. `phones` comma-separated. `sender` optional (must be pre-approved).",
          {"type": "object", "properties": {"login": {"type": "string"}, "password": {"type": "string"}, "phones": {"type": "string"}, "mes": {"type": "string"}, "sender": {"type": "string"}}, "required": ["login", "password", "phones", "mes"]}),
    _tool("smsc_balance", messaging.smsc_balance, "drive.read",
          "SMSC.ru balance.",
          {"type": "object", "properties": {"login": {"type": "string"}, "password": {"type": "string"}}, "required": ["login", "password"]}),
    _tool("smsc_status", messaging.smsc_status, "drive.read",
          "SMSC.ru per-message status.",
          {"type": "object", "properties": {"login": {"type": "string"}, "password": {"type": "string"}, "phone": {"type": "string"}, "sms_id": {"type": "string"}}, "required": ["login", "password", "phone", "sms_id"]}),
    _tool("tg_send_message", messaging.tg_send_message, "gmail.send",
          "Post a message to a Telegram chat via Bot API. parse_mode: HTML or MarkdownV2.",
          {"type": "object", "properties": {"bot_token": {"type": "string"}, "chat_id": {"oneOf": [{"type": "integer"}, {"type": "string"}]}, "text": {"type": "string"}, "parse_mode": {"type": "string"}, "disable_web_page_preview": {"type": "boolean", "default": True}}, "required": ["bot_token", "chat_id", "text"]}),
    _tool("tg_send_photo", messaging.tg_send_photo, "gmail.send",
          "Send a photo to Telegram chat by URL.",
          {"type": "object", "properties": {"bot_token": {"type": "string"}, "chat_id": {"oneOf": [{"type": "integer"}, {"type": "string"}]}, "photo_url": {"type": "string"}, "caption": {"type": "string"}}, "required": ["bot_token", "chat_id", "photo_url"]}),
    _tool("tg_get_updates", messaging.tg_get_updates, "drive.read",
          "Poll for incoming Telegram bot updates. `offset` = last update_id + 1.",
          {"type": "object", "properties": {"bot_token": {"type": "string"}, "offset": {"type": "integer", "default": 0}, "timeout": {"type": "integer", "default": 30}}, "required": ["bot_token"]}),
    _tool("tg_get_me", messaging.tg_get_me, "drive.read",
          "Verify Telegram bot token; returns bot identity.",
          {"type": "object", "properties": {"bot_token": {"type": "string"}}, "required": ["bot_token"]}),
    _tool("imap_recent", messaging.imap_recent, "drive.read",
          "List recent IMAP messages in a folder. Returns headers only — use imap_fetch_body for one message.",
          {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}, "user": {"type": "string"}, "password": {"type": "string"}, "folder": {"type": "string", "default": "INBOX"}, "since_days": {"type": "integer", "default": 1}, "use_ssl": {"type": "boolean", "default": True}, "limit": {"type": "integer", "default": 20}}, "required": ["host", "port", "user", "password"]}),
    _tool("imap_fetch_body", messaging.imap_fetch_body, "drive.read",
          "Fetch one IMAP message body + attachment list (size/filename, no payload).",
          {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}, "user": {"type": "string"}, "password": {"type": "string"}, "uid": {"type": "string"}, "folder": {"type": "string", "default": "INBOX"}, "use_ssl": {"type": "boolean", "default": True}}, "required": ["host", "port", "user", "password", "uid"]}),
    # --- Payments: ЮKassa + Тинькофф (Batch 9: 9 tools) ---
    _tool("yookassa_payments_list", payments.yookassa_payments_list, "drive.read",
          "List ЮKassa payments. status: pending, waiting_for_capture, succeeded, canceled.",
          {"type": "object", "properties": {"shop_id": {"type": "string"}, "secret": {"type": "string"}, "created_gte": {"type": "string"}, "created_lte": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer", "default": 100}, "cursor": {"type": "string"}}, "required": ["shop_id", "secret"]}),
    _tool("yookassa_payment_get", payments.yookassa_payment_get, "drive.read",
          "ЮKassa one payment by id.",
          {"type": "object", "properties": {"shop_id": {"type": "string"}, "secret": {"type": "string"}, "payment_id": {"type": "string"}}, "required": ["shop_id", "secret", "payment_id"]}),
    _tool("yookassa_refunds_list", payments.yookassa_refunds_list, "drive.read",
          "ЮKassa refunds list.",
          {"type": "object", "properties": {"shop_id": {"type": "string"}, "secret": {"type": "string"}, "created_gte": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, "required": ["shop_id", "secret"]}),
    _tool("yookassa_payouts_list", payments.yookassa_payouts_list, "drive.read",
          "ЮKassa payouts (settlement money out).",
          {"type": "object", "properties": {"shop_id": {"type": "string"}, "secret": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, "required": ["shop_id", "secret"]}),
    _tool("yookassa_receipts_list", payments.yookassa_receipts_list, "drive.read",
          "ЮKassa fiscal receipts (54-ФЗ).",
          {"type": "object", "properties": {"shop_id": {"type": "string"}, "secret": {"type": "string"}, "created_gte": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, "required": ["shop_id", "secret"]}),
    _tool("tinkoff_get_state", payments.tinkoff_get_state, "drive.read",
          "Tinkoff /GetState — single-payment status. Returns Status + ErrorCode.",
          {"type": "object", "properties": {"terminal_key": {"type": "string"}, "password": {"type": "string"}, "payment_id": {"type": "string"}}, "required": ["terminal_key", "password", "payment_id"]}),
    _tool("tinkoff_get_customer", payments.tinkoff_get_customer, "drive.read",
          "Tinkoff /GetCustomer — saved-card / customer profile.",
          {"type": "object", "properties": {"terminal_key": {"type": "string"}, "password": {"type": "string"}, "customer_key": {"type": "string"}}, "required": ["terminal_key", "password", "customer_key"]}),
    _tool("tinkoff_check_order", payments.tinkoff_check_order, "drive.read",
          "Tinkoff /CheckOrder — every payment attempt for OrderId.",
          {"type": "object", "properties": {"terminal_key": {"type": "string"}, "password": {"type": "string"}, "order_id": {"type": "string"}}, "required": ["terminal_key", "password", "order_id"]}),
    _tool("tinkoff_get_terminal_payouts", payments.tinkoff_get_terminal_payouts, "drive.read",
          "Tinkoff /GetTerminalPayouts — settlement money out for date range. Dates `2026-05-01`.",
          {"type": "object", "properties": {"terminal_key": {"type": "string"}, "password": {"type": "string"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}}, "required": ["terminal_key", "password", "from_date", "to_date"]}),
    # --- Avito + VK (Batch 10: 14 tools) ---
    _tool("avito_auth", social.avito_auth, "drive.read",
          "Avito OAuth2 client_credentials → access_token (~24h).",
          {"type": "object", "properties": {"client_id": {"type": "string"}, "client_secret": {"type": "string"}}, "required": ["client_id", "client_secret"]}),
    _tool("avito_self_info", social.avito_self_info, "drive.read",
          "Avito seller account info.",
          {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}),
    _tool("avito_user_items", social.avito_user_items, "drive.read",
          "Avito listings of a seller. status: active / removed / old / blocked / rejected.",
          {"type": "object", "properties": {"token": {"type": "string"}, "user_id": {"type": "integer"}, "per_page": {"type": "integer", "default": 100}, "page": {"type": "integer", "default": 1}, "status": {"type": "string", "default": "active"}}, "required": ["token", "user_id"]}),
    _tool("avito_balance", social.avito_balance, "drive.read",
          "Avito wallet balance.",
          {"type": "object", "properties": {"token": {"type": "string"}, "user_id": {"type": "integer"}}, "required": ["token", "user_id"]}),
    _tool("avito_messenger_chats", social.avito_messenger_chats, "drive.read",
          "Avito messenger chats list.",
          {"type": "object", "properties": {"token": {"type": "string"}, "user_id": {"type": "integer"}, "limit": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0}}, "required": ["token", "user_id"]}),
    _tool("avito_messenger_messages", social.avito_messenger_messages, "drive.read",
          "Avito messages in one chat.",
          {"type": "object", "properties": {"token": {"type": "string"}, "user_id": {"type": "integer"}, "chat_id": {"type": "string"}, "limit": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0}}, "required": ["token", "user_id", "chat_id"]}),
    _tool("avito_send_message", social.avito_send_message, "gmail.send",
          "Send Avito chat message.",
          {"type": "object", "properties": {"token": {"type": "string"}, "user_id": {"type": "integer"}, "chat_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["token", "user_id", "chat_id", "text"]}),
    _tool("vk_users_get", social.vk_users_get, "drive.read",
          "Resolve VK user IDs / screen-names to profile data.",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "user_ids": {"type": "array", "items": {"type": "string"}}, "fields": {"type": "string", "default": "city,bdate,sex"}}, "required": ["access_token", "user_ids"]}),
    _tool("vk_groups_get_members", social.vk_groups_get_members, "drive.read",
          "VK group members list.",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "group_id": {"type": "string"}, "offset": {"type": "integer", "default": 0}, "count": {"type": "integer", "default": 1000}}, "required": ["access_token", "group_id"]}),
    _tool("vk_wall_get", social.vk_wall_get, "drive.read",
          "VK wall posts. owner_id negative = group, positive = user.",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "owner_id": {"type": "integer"}, "count": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0}}, "required": ["access_token", "owner_id"]}),
    _tool("vk_wall_post", social.vk_wall_post, "gmail.send",
          "VK wall post.",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "owner_id": {"type": "integer"}, "message": {"type": "string"}, "attachments": {"type": "string"}}, "required": ["access_token", "owner_id", "message"]}),
    _tool("vk_messages_send", social.vk_messages_send, "gmail.send",
          "VK private message. peer_id: user / chat(2000000000+id) / group(-id).",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "peer_id": {"type": "integer"}, "message": {"type": "string"}, "random_id": {"type": "integer", "default": 0}}, "required": ["access_token", "peer_id", "message"]}),
    _tool("vk_ads_get_campaigns", social.vk_ads_get_campaigns, "drive.read",
          "VK ad campaigns list.",
          {"type": "object", "properties": {"access_token": {"type": "string"}, "account_id": {"type": "integer"}}, "required": ["access_token", "account_id"]}),
    # --- СБИС + Контур.Диадок ЭДО (Batch 11: 8 tools) ---
    _tool("sbis_auth", edo.sbis_auth, "drive.read",
          "СБИС.Аутентифицировать — login+password → session_id.",
          {"type": "object", "properties": {"login": {"type": "string"}, "password": {"type": "string"}}, "required": ["login", "password"]}),
    _tool("sbis_docs_list", edo.sbis_docs_list, "drive.read",
          "СБИС.СписокДокументов. doc_type: ВходящийДокумент / ИсходящийДокумент. Dates DD.MM.YYYY.",
          {"type": "object", "properties": {"session_id": {"type": "string"}, "doc_type": {"type": "string", "default": "ВходящийДокумент"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}, "limit": {"type": "integer", "default": 50}}, "required": ["session_id"]}),
    _tool("sbis_doc_get", edo.sbis_doc_get, "drive.read",
          "СБИС single document detail.",
          {"type": "object", "properties": {"session_id": {"type": "string"}, "doc_id": {"type": "string"}}, "required": ["session_id", "doc_id"]}),
    _tool("sbis_changes_since", edo.sbis_changes_since, "drive.read",
          "СБИС.СписокИзменений for delta sync. since_iso ISO8601 with timezone.",
          {"type": "object", "properties": {"session_id": {"type": "string"}, "since_iso": {"type": "string"}}, "required": ["session_id", "since_iso"]}),
    _tool("diadoc_authenticate", edo.diadoc_authenticate, "drive.read",
          "Контур.Диадок password auth → auth_token.",
          {"type": "object", "properties": {"api_key": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}}, "required": ["api_key", "login", "password"]}),
    _tool("diadoc_my_organizations", edo.diadoc_my_organizations, "drive.read",
          "Диадок: orgs the user has access to.",
          {"type": "object", "properties": {"api_key": {"type": "string"}, "auth_token": {"type": "string"}}, "required": ["api_key", "auth_token"]}),
    _tool("diadoc_docs_list", edo.diadoc_docs_list, "drive.read",
          "Диадок docs list. filter_category: Any.Inbound / Any.Outbound / UniversalTransferDocument.Inbound.NotFinished etc. Dates dd.MM.yyyy.",
          {"type": "object", "properties": {"api_key": {"type": "string"}, "auth_token": {"type": "string"}, "box_id": {"type": "string"}, "filter_category": {"type": "string", "default": "Any.Inbound"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}}, "required": ["api_key", "auth_token", "box_id"]}),
    _tool("diadoc_get_event", edo.diadoc_get_event, "drive.read",
          "Диадок one event (document delivery / signature).",
          {"type": "object", "properties": {"api_key": {"type": "string"}, "auth_token": {"type": "string"}, "box_id": {"type": "string"}, "message_id": {"type": "string"}}, "required": ["api_key", "auth_token", "box_id", "message_id"]}),
    # --- ML helpers (Batch 12: 11 tools) ---
    _tool("nlp_extract_inns", mlhelpers.nlp_extract_inns, "local.read",
          "Extract Russian INN (10 or 12 digit) from text. validate=True keeps only valid FNS checksums.",
          {"type": "object", "properties": {"text": {"type": "string"}, "validate": {"type": "boolean", "default": True}}, "required": ["text"]}),
    _tool("nlp_extract_phones", mlhelpers.nlp_extract_phones, "local.read",
          "Extract Russian phone numbers; normalize=True → E.164-like `79991234567`.",
          {"type": "object", "properties": {"text": {"type": "string"}, "normalize": {"type": "boolean", "default": True}}, "required": ["text"]}),
    _tool("nlp_extract_bik", mlhelpers.nlp_extract_bik, "local.read",
          "Extract Russian bank BIC codes (start with 04, 9 digits).",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    _tool("nlp_extract_ogrn", mlhelpers.nlp_extract_ogrn, "local.read",
          "Extract Russian OGRN/OGRNIP (13 or 15 digits).",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    _tool("nlp_named_entities", mlhelpers.nlp_named_entities, "local.read",
          "Full Natasha NER pass (org / person / location). Lazy-imports natasha; returns hint if not installed.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    _tool("dadata_suggest_address", mlhelpers.dadata_suggest_address, "drive.read",
          "DaData address autocomplete (КЛАДР/ФИАС-backed).",
          {"type": "object", "properties": {"token": {"type": "string"}, "query": {"type": "string"}, "count": {"type": "integer", "default": 10}}, "required": ["token", "query"]}),
    _tool("dadata_clean_address", mlhelpers.dadata_clean_address, "drive.read",
          "DaData full address cleaning + geocoding (paid endpoint, needs secret).",
          {"type": "object", "properties": {"token": {"type": "string"}, "secret": {"type": "string"}, "address": {"type": "string"}}, "required": ["token", "secret", "address"]}),
    _tool("dadata_suggest_party", mlhelpers.dadata_suggest_party, "drive.read",
          "DaData company / IP autocomplete by name or INN.",
          {"type": "object", "properties": {"token": {"type": "string"}, "query": {"type": "string"}, "count": {"type": "integer", "default": 10}}, "required": ["token", "query"]}),
    _tool("dadata_find_party_by_inn", mlhelpers.dadata_find_party_by_inn, "drive.read",
          "DaData lookup company/IP by exact INN.",
          {"type": "object", "properties": {"token": {"type": "string"}, "inn": {"type": "string"}}, "required": ["token", "inn"]}),
    _tool("dadata_suggest_bank", mlhelpers.dadata_suggest_bank, "drive.read",
          "DaData bank autocomplete by name or BIC.",
          {"type": "object", "properties": {"token": {"type": "string"}, "query": {"type": "string"}, "count": {"type": "integer", "default": 10}}, "required": ["token", "query"]}),
    _tool("embed_texts", mlhelpers.embed_texts, "local.read",
          "Embed texts with multilingual sentence-transformer. Default model: multilingual-e5-small.",
          {"type": "object", "properties": {"texts": {"type": "array", "items": {"type": "string"}}, "model": {"type": "string", "default": "intfloat/multilingual-e5-small"}}, "required": ["texts"]}),
    _tool("cosine_similarity", mlhelpers.cosine_similarity, "local.read",
          "Cosine similarity between two equal-length vectors.",
          {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "number"}}, "b": {"type": "array", "items": {"type": "number"}}}, "required": ["a", "b"]}),
    _tool("ocr_image", mlhelpers.ocr_image, "local.read",
          "OCR an image. engine: tesseract (default, local) or paddle (more accurate on Cyrillic).",
          {"type": "object", "properties": {"image_path": {"type": "string"}, "lang": {"type": "string", "default": "rus+eng"}, "engine": {"type": "string", "default": "tesseract"}}, "required": ["image_path"]}),
    _tool("ocr_pdf", mlhelpers.ocr_pdf, "local.read",
          "OCR every page of a scanned PDF. For digitally-born PDFs prefer file_extract.",
          {"type": "object", "properties": {"pdf_path": {"type": "string"}, "lang": {"type": "string", "default": "rus+eng"}}, "required": ["pdf_path"]}),
    _tool("pandera_validate", mlhelpers.pandera_validate, "local.read",
          "Validate a list of dict-records against a Pandera DataFrameSchema (JSON-encoded). Returns row-level errors.",
          {"type": "object", "properties": {"records": {"type": "array", "items": {"type": "object"}}, "schema_json": {"type": "string"}}, "required": ["records", "schema_json"]}),
    # --- DuckDB local analytics (Batch 13: 5 tools) ---
    _tool("duckdb_query", analytics_local.duckdb_query, "local.read",
          "Run a SQL query against the local DuckDB. Supports `read_csv_auto('path')` inline. max_rows caps output.",
          {"type": "object", "properties": {"sql": {"type": "string"}, "max_rows": {"type": "integer", "default": 1000}}, "required": ["sql"]}),
    _tool("duckdb_import_csv", analytics_local.duckdb_import_csv, "local.write",
          "Import a local CSV into a DuckDB table. replace=True overwrites.",
          {"type": "object", "properties": {"table": {"type": "string"}, "path": {"type": "string"}, "replace": {"type": "boolean", "default": False}}, "required": ["table", "path"]}),
    _tool("duckdb_list_tables", analytics_local.duckdb_list_tables, "local.read",
          "List all tables + row counts + columns in the local DuckDB.",
          {"type": "object", "properties": {}}),
    _tool("duckdb_drop_table", analytics_local.duckdb_drop_table, "local.delete",
          "Drop a DuckDB table.",
          {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}),
    _tool("duckdb_export_parquet", analytics_local.duckdb_export_parquet, "local.write",
          "Export a DuckDB table to a Parquet file.",
          {"type": "object", "properties": {"table": {"type": "string"}, "path": {"type": "string"}}, "required": ["table", "path"]}),
    # --- 1С OData (Batch 14: 5 tools) ---
    _tool("onec_odata_query", onec.onec_odata_query, "drive.read",
          "Generic 1С OData GET. `path`: entity name (e.g. Catalog_Контрагенты). filter_: OData filter syntax.",
          {"type": "object", "properties": {"base_url": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "path": {"type": "string"}, "filter_": {"type": "string"}, "top": {"type": "integer", "default": 100}, "skip": {"type": "integer", "default": 0}, "select": {"type": "string"}}, "required": ["base_url", "login", "password", "path"]}),
    _tool("onec_contractors", onec.onec_contractors, "drive.read",
          "1С Catalog_Контрагенты filtered by name substring.",
          {"type": "object", "properties": {"base_url": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "name_like": {"type": "string"}, "top": {"type": "integer", "default": 100}}, "required": ["base_url", "login", "password"]}),
    _tool("onec_products", onec.onec_products, "drive.read",
          "1С Catalog_Номенклатура.",
          {"type": "object", "properties": {"base_url": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "top": {"type": "integer", "default": 100}, "skip": {"type": "integer", "default": 0}}, "required": ["base_url", "login", "password"]}),
    _tool("onec_documents", onec.onec_documents, "drive.read",
          "1С documents. doc_type example: Document_РеализацияТоваровУслуг. date_from OData datetime `2026-05-01T00:00:00`.",
          {"type": "object", "properties": {"base_url": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "doc_type": {"type": "string"}, "date_from": {"type": "string"}, "top": {"type": "integer", "default": 100}}, "required": ["base_url", "login", "password", "doc_type"]}),
    _tool("onec_money_balance", onec.onec_money_balance, "drive.read",
          "1С AccumulationRegister_ДенежныеСредстваБалансе — cash balance snapshot.",
          {"type": "object", "properties": {"base_url": {"type": "string"}, "login": {"type": "string"}, "password": {"type": "string"}, "date_iso": {"type": "string"}}, "required": ["base_url", "login", "password"]}),
    # --- MDM (Batch 15: 4 tools) ---
    _tool("mdm_table_get", infra.mdm_table_get, "local.read",
          "Read entire MDM table (products / suppliers / contractors).",
          {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}),
    _tool("mdm_record_upsert", infra.mdm_record_upsert, "local.write",
          "Insert or merge an MDM record by id. external_ids carries marketplace cross-refs (wb_nm, ozon_sku).",
          {"type": "object", "properties": {"table": {"type": "string"}, "record_id": {"type": "string"}, "fields": {"type": "object"}, "external_ids": {"type": "object"}}, "required": ["table", "record_id", "fields"]}),
    _tool("mdm_resolve", infra.mdm_resolve, "local.read",
          "Find an MDM record by external id (e.g. wb_nm). Returns the first match.",
          {"type": "object", "properties": {"table": {"type": "string"}, "external_key": {"type": "string"}, "external_value": {"type": "string"}}, "required": ["table", "external_key", "external_value"]}),
    _tool("mdm_delete", infra.mdm_delete, "local.delete",
          "Remove an MDM record by id.",
          {"type": "object", "properties": {"table": {"type": "string"}, "record_id": {"type": "string"}}, "required": ["table", "record_id"]}),
    # --- Approvals (Batch 16: 4 tools) ---
    _tool("approval_request", infra.approval_request, "local.write",
          "Stage an approval request for a destructive action. Returns {approval_id}. Caller polls approval_status; when 'approved', the destructive op may run.",
          {"type": "object", "properties": {"action": {"type": "string"}, "args": {"type": "object"}, "requested_by": {"type": "string", "default": "agent"}, "reason": {"type": "string"}}, "required": ["action", "args"]}),
    _tool("approval_decide", infra.approval_decide, "local.write",
          "Approve or deny a pending request. status: approved | denied.",
          {"type": "object", "properties": {"approval_id": {"type": "string"}, "status": {"type": "string", "enum": ["approved", "denied"]}, "decided_by": {"type": "string", "default": "user"}, "note": {"type": "string"}}, "required": ["approval_id", "status"]}),
    _tool("approval_status", infra.approval_status, "local.read",
          "Latest status of an approval (returns the most recent decision row).",
          {"type": "object", "properties": {"approval_id": {"type": "string"}}, "required": ["approval_id"]}),
    _tool("approval_list", infra.approval_list, "local.read",
          "List recent approvals. status: pending | approved | denied | any.",
          {"type": "object", "properties": {"status": {"type": "string"}, "limit": {"type": "integer", "default": 50}}}),
    # --- Audit log (Batch 17: 2 tools) ---
    _tool("audit_log", infra.audit_log, "local.write",
          "Append a row to the local audit log. Tools should call this just before/after every destructive action with the args summary.",
          {"type": "object", "properties": {"action": {"type": "string"}, "tool": {"type": "string"}, "args": {"type": "object"}, "actor": {"type": "string", "default": "agent"}, "result_summary": {"type": "string"}, "correlation_id": {"type": "string"}}, "required": ["action", "tool", "args"]}),
    _tool("audit_search", infra.audit_search, "local.read",
          "Search audit log by actor / tool / action / since timestamp. Latest-first.",
          {"type": "object", "properties": {"actor": {"type": "string"}, "tool": {"type": "string"}, "action": {"type": "string"}, "since_iso": {"type": "string"}, "limit": {"type": "integer", "default": 100}}}),
    # --- BI dashboard (Batch 18: 3 tools) ---
    _tool("bi_dashboard_render", infra.bi_dashboard_render, "local.write",
          "Render a one-page self-contained HTML dashboard. kpis = [{label, value, delta?, unit?}].",
          {"type": "object", "properties": {"title": {"type": "string"}, "kpis": {"type": "array", "items": {"type": "object"}}, "html_path": {"type": "string"}}, "required": ["title", "kpis", "html_path"]}),
    _tool("bi_kpi_history_log", infra.bi_kpi_history_log, "local.write",
          "Append a KPI value to local history for trend charts.",
          {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "number"}, "ts": {"type": "string"}, "tags": {"type": "object"}}, "required": ["name", "value"]}),
    _tool("bi_kpi_history_get", infra.bi_kpi_history_get, "local.read",
          "Recent KPI history points for a named series.",
          {"type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "default": 1000}}, "required": ["name"]}),
    # --- Scheduler hints (Batch 19: 4 tools) ---
    _tool("scheduler_enqueue", infra.scheduler_enqueue, "local.write",
          "Record a scheduled task. This is a hint — the harness must poll scheduler_due.",
          {"type": "object", "properties": {"task": {"type": "string"}, "run_at_iso": {"type": "string"}, "payload": {"type": "object"}}, "required": ["task", "run_at_iso"]}),
    _tool("scheduler_due", infra.scheduler_due, "local.read",
          "List pending tasks with run_at ≤ until_iso (default now).",
          {"type": "object", "properties": {"until_iso": {"type": "string"}}}),
    _tool("scheduler_complete", infra.scheduler_complete, "local.write",
          "Mark a scheduled task done.",
          {"type": "object", "properties": {"task_id": {"type": "string"}, "result_note": {"type": "string"}}, "required": ["task_id"]}),
    _tool("scheduler_cancel", infra.scheduler_cancel, "local.write",
          "Cancel a pending scheduled task.",
          {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}),
    # --- Skill registry (Batch 20: 3 tools) ---
    _tool("skill_register", infra.skill_register, "local.write",
          "Register a named skill — a bundle of tool names + prose. Builds an index of capabilities.",
          {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "description", "tools"]}),
    _tool("skill_list", infra.skill_list, "local.read",
          "List registered skills (optional tag filter).",
          {"type": "object", "properties": {"tag": {"type": "string"}}}),
    _tool("skill_remove", infra.skill_remove, "local.delete",
          "Remove a skill from the registry.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    # --- Label printing (Batch 21: 3 tools) ---
    _tool("zpl_render_label", infra.zpl_render_label, "local.write",
          "Substitute {field} placeholders in a ZPL template and write to disk. Send the file to a Zebra ZPL printer.",
          {"type": "object", "properties": {"template": {"type": "string"}, "fields": {"type": "object"}, "out_path": {"type": "string"}}, "required": ["template", "fields", "out_path"]}),
    _tool("tspl_render_label", infra.tspl_render_label, "local.write",
          "Same as zpl_render_label but for TSPL (Godex / TSC).",
          {"type": "object", "properties": {"template": {"type": "string"}, "fields": {"type": "object"}, "out_path": {"type": "string"}}, "required": ["template", "fields", "out_path"]}),
    _tool("zpl_render_wb_label", infra.zpl_render_wb_label, "local.write",
          "Pre-baked WB FBS shipping label ZPL template — fills barcode + sku + supplier + weight, writes to disk.",
          {"type": "object", "properties": {"barcode": {"type": "string"}, "sku": {"type": "string"}, "supplier": {"type": "string"}, "weight_g": {"type": "integer"}, "out_path": {"type": "string"}}, "required": ["barcode", "sku", "supplier", "weight_g", "out_path"]}),
    # --- Service layer: webhooks + locks + tracing + notifications + reports (Batch 22: 13 tools) ---
    _tool("webhook_log", service.webhook_log, "local.write",
          "Append an incoming webhook payload to the log. source: yookassa, tinkoff, telegram, wb_finance_notify, etc.",
          {"type": "object", "properties": {"source": {"type": "string"}, "payload": {"type": "object"}, "headers": {"type": "object"}, "signature_valid": {"type": "boolean"}}, "required": ["source", "payload"]}),
    _tool("webhook_recent", service.webhook_recent, "local.read",
          "Recent webhooks, latest first.",
          {"type": "object", "properties": {"source": {"type": "string"}, "limit": {"type": "integer", "default": 50}}}),
    _tool("webhook_verify_signature", service.webhook_verify_signature, "local.read",
          "Verify HMAC-{algorithm} signature on a raw body. Used to validate ЮKassa / Tinkoff / WB callbacks.",
          {"type": "object", "properties": {"secret": {"type": "string"}, "raw_body": {"type": "string"}, "received_signature": {"type": "string"}, "algorithm": {"type": "string", "default": "sha256"}}, "required": ["secret", "raw_body", "received_signature"]}),
    _tool("lock_acquire", service.lock_acquire, "local.write",
          "Acquire a named lock (thread + file marker). Use to serialize destructive ops across parallel agent turns.",
          {"type": "object", "properties": {"name": {"type": "string"}, "ttl_seconds": {"type": "integer", "default": 300}, "wait_seconds": {"type": "integer", "default": 0}, "owner": {"type": "string", "default": "agent"}}, "required": ["name"]}),
    _tool("lock_release", service.lock_release, "local.write",
          "Release a lock by its token (mismatch rejected).",
          {"type": "object", "properties": {"name": {"type": "string"}, "token": {"type": "string"}}, "required": ["name", "token"]}),
    _tool("lock_status", service.lock_status, "local.read",
          "Inspect a lock without acquiring.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    _tool("trace_span_log", service.trace_span_log, "local.write",
          "Append a span to the local trace log. OpenTelemetry-shaped {span_id, parent_span_id, name, duration_ms, attributes}.",
          {"type": "object", "properties": {"span_name": {"type": "string"}, "duration_ms": {"type": "number"}, "attributes": {"type": "object"}, "parent_span_id": {"type": "string"}}, "required": ["span_name", "duration_ms"]}),
    _tool("trace_recent", service.trace_recent, "local.read",
          "Recent spans, substring + since filters.",
          {"type": "object", "properties": {"name_like": {"type": "string"}, "since_iso": {"type": "string"}, "limit": {"type": "integer", "default": 100}}}),
    _tool("notify_route", service.notify_route, "local.write",
          "Stage a notification. level: info | warning | error | critical. channels: ['telegram_ops','email_finance',...].",
          {"type": "object", "properties": {"level": {"type": "string", "enum": ["info", "warning", "error", "critical"]}, "message": {"type": "string"}, "channels": {"type": "array", "items": {"type": "string"}}}, "required": ["level", "message"]}),
    _tool("notify_mark_delivered", service.notify_mark_delivered, "local.write",
          "Record that a notification was actually sent on a channel.",
          {"type": "object", "properties": {"notification_id": {"type": "string"}, "channel": {"type": "string"}, "result": {"type": "string"}}, "required": ["notification_id", "channel"]}),
    _tool("report_render_markdown", service.report_render_markdown, "local.write",
          "Render a markdown report. sections = [{heading, body}]. Body is plain markdown.",
          {"type": "object", "properties": {"title": {"type": "string"}, "sections": {"type": "array", "items": {"type": "object"}}, "out_path": {"type": "string"}}, "required": ["title", "sections", "out_path"]}),
    _tool("report_render_csv", service.report_render_csv, "local.write",
          "Render a CSV report. headers + rows. Returns {path, bytes, row_count}.",
          {"type": "object", "properties": {"headers": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array", "items": {"type": "array"}}, "out_path": {"type": "string"}}, "required": ["headers", "rows", "out_path"]}),
    _tool("team_channel_send", service.team_channel_send, "local.write",
          "Unified team-channel dispatcher. Stages a notification + returns routing decision pointing at the right send tool (tg_send_message / gmail_create_draft / smsru_send). Centralizes channel selection.",
          {"type": "object", "properties": {"channel": {"type": "string", "description": "Prefixed: telegram_ops, email_finance, sms_alerts, ..."}, "message": {"type": "string"}, "level": {"type": "string", "enum": ["info", "warning", "error", "critical"], "default": "info"}, "attachments": {"type": "array", "items": {"type": "object"}}}, "required": ["channel", "message"]}),
    # --- Sheets helpers ---
    _tool(
        "sheets_last_data_row",
        sheets.last_data_row,
        "sheets.read",
        "Find the last non-empty row in a column. Unlike summarize().grid.rows (which is sheet DIMENSION, often inflated), this is the actual data extent.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string"},
                "column": {"type": "string", "default": "A"},
            },
            "required": ["spreadsheet_id", "sheet"],
        },
    ),
    _tool(
        "sheets_snapshot_range",
        sheets.snapshot_range,
        "sheets.read",
        "Take a structural snapshot of a sheet range (all values + dimensions). Cheap, one read. Pair with sheets_diff_snapshot(before, after) to verify what a script wrote.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range"],
        },
    ),
    _tool(
        "sheets_diff_snapshot",
        sheets.diff_snapshot,
        "sheets.read",
        "Compare two sheets_snapshot_range() results. Returns {rows_added, rows_removed, cells_changed, diff_examples, new_tail_rows}.",
        {
            "type": "object",
            "properties": {
                "before": {"type": "object"},
                "after": {"type": "object"},
                "max_examples": {"type": "integer", "default": 10},
            },
            "required": ["before", "after"],
        },
    ),
    # --- Apps Script: smart-run + triggers ---
    _tool(
        "apps_script_api_run_smart",
        apps_script_api.run_smart,
        "apps_script.run",
        "Cascade run: tries scripts.run dev → scripts.run pinned → Playwright custom-menu click. Use when the script is bound to a spreadsheet whose GCP project might not match ours. Pass custom_menu_path (e.g. ['☰ WB', 'API', 'Фин.отчеты']) to enable the menu fallback.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "function_name": {"type": "string", "default": "main"},
                "params": {"type": "array"},
                "spreadsheet_id": {"type": "string", "description": "For Playwright menu fallback"},
                "custom_menu_path": {"type": "array", "items": {"type": "string"}},
                "wait_after_menu_sec": {"type": "integer", "default": 300},
            },
        },
    ),
    _tool(
        "apps_script_api_triggers_install_one_shot",
        apps_script_api.triggers_install_one_shot,
        "apps_script.run",
        "Install a one-shot CLOCK trigger that fires `function_name` after `delay_minutes`. Useful for scheduling work that must run later (e.g. retry after a WB rate-limit window). Requires GCP project alignment (use browser_set_script_gcp_project if needed).",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "function_name": {"type": "string"},
                "delay_minutes": {"type": "integer", "default": 1},
            },
            "required": ["script_id", "function_name"],
        },
    ),
    _tool(
        "apps_script_api_triggers_list",
        apps_script_api.triggers_list,
        "apps_script.run",
        "List installed triggers on a script. Returns [{id, function, event_type, source}].",
        {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    ),
    _tool(
        "apps_script_api_triggers_remove",
        apps_script_api.triggers_remove,
        "apps_script.edit",
        "Remove triggers by ID or handler function name. Returns {removed_count}.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "trigger_id": {"type": "string"},
                "function_name": {"type": "string"},
            },
            "required": ["script_id"],
        },
    ),
    # --- Browser: profiles + GCP project switcher ---
    _tool(
        "browser_list_profiles",
        browser.list_profiles,
        "apps_script.edit",
        "List browser profiles configured. Each profile is an independent persistent Chromium profile, allowing different Google accounts to be logged in for different sessions.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "browser_set_script_gcp_project",
        browser.set_script_gcp_project,
        "apps_script.edit",
        "Switch an Apps Script project's GCP project to `project_number` (e.g. 148389149001 — our OAuth client). Needed for scripts.run / Cloud Logging / triggers on bound scripts. Playwright clicks Project Settings → Change project.",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "project_number": {"type": "string"},
                "headless": {"type": "boolean", "default": False, "description": "Visible first time so you can confirm the change."},
                "profile": {"type": "string", "default": "default"},
            },
            "required": ["script_id", "project_number"],
        },
    ),
    # --- GCP API enable + project listing ---
    _tool(
        "gcp_enable_api",
        gcp.enable_api,
        "apps_script.edit",
        "Enable a Google Cloud API in our GCP project via Service Usage API — no Cloud Console click needed. `api_name` is the hostname, e.g. 'driveactivity.googleapis.com', 'logging.googleapis.com', 'script.googleapis.com', 'sheets.googleapis.com'. Idempotent.",
        {
            "type": "object",
            "properties": {
                "api_name": {"type": "string"},
                "project_number": {"type": "string", "default": "148389149001"},
            },
            "required": ["api_name"],
        },
    ),
    _tool(
        "gcp_list_enabled_apis",
        gcp.list_enabled_apis,
        "apps_script.edit",
        "List all APIs enabled in our GCP project. Returns {count, apis: [...]}.",
        {
            "type": "object",
            "properties": {"project_number": {"type": "string", "default": "148389149001"}},
        },
    ),
    _tool(
        "gcp_list_projects",
        gcp.list_projects,
        "apps_script.edit",
        "List all GCP projects the calling account has access to. Returns [{project_id, project_number, name, state}].",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "gcp_project_number",
        gcp.project_number,
        "apps_script.edit",
        "Look up the numeric project_number for a project_id. Handy when you only remember the human-readable id but need the number for browser_set_script_gcp_project.",
        {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    ),
    # --- Cloud Logging — read Apps Script execution logs ---
    _tool(
        "cloud_logging_read",
        cloud_logging.read_logs,
        "apps_script.edit",
        "Read recent Cloud Logging entries with an optional advanced filter. Use this to fetch Apps Script Logger.log output without scraping the editor UI. Common filter: 'resource.type=\"app_script_function\" AND resource.labels.script_id=\"<id>\"'. Defaults to last 60 minutes.",
        {
            "type": "object",
            "properties": {
                "filter_expr": {"type": "string", "description": "Cloud Logging filter; omit for all entries."},
                "project_id": {"type": "string", "default": "148389149001"},
                "minutes_back": {"type": "integer", "default": 60},
                "page_size": {"type": "integer", "default": 100},
            },
        },
    ),
    _tool(
        "cloud_logging_script_executions",
        cloud_logging.script_executions,
        "apps_script.edit",
        "List recent function executions for a specific Apps Script. Returns one entry per execution_id with status + start time + log count. Requires the script to be linked to our GCP project (use browser_set_script_gcp_project first).",
        {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "minutes_back": {"type": "integer", "default": 60},
                "project_id": {"type": "string", "default": "148389149001"},
            },
            "required": ["script_id"],
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
        "Substring search across ALL saved chats. Returns matches with short snippets so you can decide which chat to read in full. Use when the user references prior work ('что мы делали с таблицей X на прошлой неделе'). `response_format='concise'` (default) returns chat_id+title+snippet[:200]; 'detailed' adds started_at+message_count+full snippet.",
        {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}, "required": ["query"]},
    ),
    _tool(
        "chats_search_semantic",
        chats.search_semantic,
        "chats.read",
        "Semantic search across saved chats (local embeddings). Better than chats_search for fuzzy queries ('налоги' matches 'НДС'). Falls back to substring if embedding model unavailable; `_meta.search_method` flags which.",
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
        "Semantic search across notes (local embeddings). Better than notes_search for fuzzy queries ('налоги' → notes about НДС). Falls back to substring if embedding model unavailable; `_meta.search_method` flags which.",
        {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]},
    ),
    _tool(
        "notes_remove",
        notes.remove,
        "notes.write",
        "Delete a note by id. Use when the user explicitly asks to forget something.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    ),
    # --- Phase 15: File Analysis Ensemble ---
    _tool(
        "file_extract_text",
        file_extract.extract_text,
        "files.read",
        "Universal text extraction from any supported file or Google URL. Auto-routes by extension/URL: TXT/MD/CSV/PDF/XLSX/DOCX (local), PNG/JPG (OCR via Tesseract), MP3/M4A/WAV (Whisper API, requires OPENAI_API_KEY), Google Doc URL (Docs API), Google Sheet URL (structural summary). Returns {text, file_kind, source, chars, truncated, _meta}. Use this BEFORE file_analyze_ensemble if you want raw text without LLM cost, or to peek at file size.",
        {
            "type": "object",
            "properties": {
                "path_or_url": {"type": "string", "description": "Local file path OR https URL to Google Docs/Sheets/Drive file."},
                "kind": {"type": "string", "description": "Optional override of auto-detected kind ('text'/'pdf'/'docx'/'xlsx'/'image'/'audio'/'gdoc'/'gsheet')."},
                "max_chars": {"type": "integer", "description": "Cap output at N chars. Sets _meta.truncated=true if hit."},
            },
            "required": ["path_or_url"],
        },
        category="files",
    ),
    _tool(
        "file_analyze_ensemble",
        file_analyze.analyze,
        "files.read",
        "**3-LLM ensemble file analysis** (Phase 15). Use when user attached a file (PDF/DOCX/XLSX/Image/Audio) OR pasted Google Doc/Sheet URL AND asked for analysis. **TRIGGER PHRASES (RU/EN):** проанализируй / разбери / выдай сводку / резюмируй / что главное / боли клиента / рекомендации / приоритетные действия / факторный анализ / финансовый разбор / analyze / summarize / what's the main point / pain points / recommendations. Pipeline: Haiku (facts, parallel) + Sonnet (interpretation, parallel) → Sonnet-judge sees both + 5KB excerpt → synthesis. Output: structured Russian markdown (Главное / Боли / Рекомендации / Цифры / Расхождения between passes), ₽ tables, action items. ~30-90s wall-clock. **Uses claude CLI subscription auth — NO API key needed.** Saves `.md` to .data/analyses/ + indexes via notes.add for `analyses_search` later. For non-analysis intent (просто покажи / найди в файле X) → `file_extract_text` instead.",
        {
            "type": "object",
            "properties": {
                "path_or_url": {"type": "string", "description": "Local file path OR https URL (Google Docs/Sheets)."},
                "focus": {"type": "string", "description": "What to extract/analyze (e.g. 'боли клиента + рекомендации финансиста', 'ключевые цифры и риски', 'action items'). Be specific — drives all 3 LLMs."},
                "save_as": {"type": "string", "description": "Optional name for the .md file. If omitted: auto-generated from source filename + UTC timestamp."},
                "max_chars": {"type": "integer", "description": "Cap input text before LLM. Default 100,000 chars. Lower if you want to save tokens."},
            },
            "required": ["path_or_url", "focus"],
        },
        category="files",
    ),
    _tool(
        "analyses_list",
        file_analyze.list_analyses,
        "files.read",
        "List all saved file analyses (`.data/analyses/*.md`), newest first. Returns {analyses:[{name, path, source, focus, created_at, chars_in, file_kind}]}. Use to see what previous analyses are available before searching.",
        {"type": "object", "properties": {}},
        category="files",
    ),
    _tool(
        "analyses_read",
        file_analyze.read_analysis,
        "files.read",
        "Read a saved analysis `.md` back into context. `name` is what analyses_list returns (with or without .md extension). Returns full content including YAML front-matter, synthesis, pass_a, pass_b.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Analysis name from analyses_list (e.g. 'zoom_olga_2026-05-21')."}},
            "required": ["name"],
        },
        category="files",
    ),
    _tool(
        "analyses_search",
        file_analyze.search_analyses,
        "files.read",
        "Semantic search across saved file analyses (subset of notes filtered by tag analysis:). Use when user asks about previous file analyses ('что мы вытащили из созвона Ольги?', 'какие боли мы находили у Иванова?'). Returns top_k hits with preview snippets; follow up with analyses_read(name) to get full .md.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query in the user's language."},
                "top_k": {"type": "integer", "description": "Max results. Default 5."},
            },
            "required": ["query"],
        },
        category="files",
    ),
    # --- Aliases (local name→account registry; NOT Google Contacts) ---
    _tool(
        "aliases_list",
        aliases.list_all,
        "aliases.read",
        "List all entries in the local alias registry. Each entry binds one or more human names (and optionally an email) to a Google account alias. Distinct from Google Contacts (contacts_*).",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "aliases_resolve",
        aliases.resolve,
        "aliases.read",
        "Resolve free-text ('Лена', 'партнёр', email) → registry entries. Call FIRST when user mentions a person by name. One hit → use .account. Multiple → ask to disambiguate. Zero → ask + aliases_add.",
        {"type": "object", "properties": {"hint": {"type": "string"}}, "required": ["hint"]},
    ),
    _tool(
        "aliases_add",
        aliases.add,
        "aliases.write",
        "Register a name→account binding or merge new info into an existing entry. Bind multiple names (including nicknames) to one account alias. Call proactively when the user introduces a new person.",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "OAuth alias (must already exist via auth_add_account)."},
                "names": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "email": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["account", "names"],
        },
    ),
    _tool(
        "aliases_remove",
        aliases.remove,
        "aliases.write",
        "Drop an alias binding by account.",
        {"type": "object", "properties": {"account": {"type": "string"}}, "required": ["account"]},
    ),
    # --- Gmail ---
    _tool(
        "gmail_search",
        gmail.search,
        "gmail.read",
        "Search emails via Gmail query syntax: 'from:elena', 'has:attachment', 'subject:invoice', 'newer_than:7d'. Returns {messages, _meta:{total_count, truncated}}. **Default max_results=20** (cap 100); `_meta.total_count` is Gmail's estimate. If truncated, narrow query or raise max_results. `response_format='concise'` (default) returns id+from+subject+date+snippet[:120]; 'detailed' adds to/thread_id/full snippet/labels.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "Default 20, max 100."},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
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
        "List Gmail labels (system + user-created). Useful for narrowing searches with 'label:foo' and for finding label IDs to pass to gmail_modify_labels.",
        {"type": "object", "properties": {}},
    ),
    # --- Phase 5: Gmail write-ops ---
    _tool(
        "gmail_get_thread",
        gmail.get_thread,
        "gmail.read",
        "Read every message in a thread (oldest → newest). Returns {thread_id, messages: [{id, from, to, subject, date, snippet, body_text, ...}], _meta}. Critical context before replying to a multi-message conversation.",
        {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]},
    ),
    _tool(
        "gmail_reply",
        gmail.reply,
        "gmail.draft",
        "Create a DRAFT reply to a message with correct threading headers (In-Reply-To, References). reply_all=true includes original To+Cc. Never sends — caller uses gmail_send_draft after user approval.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "body": {"type": "string"},
                "reply_all": {"type": "boolean"},
            },
            "required": ["message_id", "body"],
        },
    ),
    _tool(
        "gmail_forward",
        gmail.forward,
        "gmail.draft",
        "Create a DRAFT forward of a message with original headers + body quoted below. Optional `body` is inserted before the quote.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "to": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
            },
            "required": ["message_id", "to"],
        },
    ),
    _tool(
        "gmail_modify_labels",
        gmail.modify_labels,
        "gmail.write",
        "Add or remove labels on a message. `add`/`remove` are lists of label IDs (use gmail_list_labels to resolve names to ids). System ids: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "add": {"type": "array", "items": {"type": "string"}},
                "remove": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["message_id"],
        },
    ),
    _tool(
        "gmail_archive",
        gmail.archive,
        "gmail.write",
        "Archive a message (remove INBOX label).",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
    _tool(
        "gmail_mark_read",
        gmail.mark_read,
        "gmail.write",
        "Mark a message as read (remove UNREAD label).",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
    _tool(
        "gmail_mark_unread",
        gmail.mark_unread,
        "gmail.write",
        "Mark a message as unread (add UNREAD label).",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
    _tool(
        "gmail_batch_modify",
        gmail.batch_modify,
        "gmail.write",
        "Bulk label modify across many messages in ONE call. Use for «архивировать все письма от X старше года»: gmail_search → extract ids → batch_modify(remove=['INBOX']).",
        {
            "type": "object",
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "string"}},
                "add": {"type": "array", "items": {"type": "string"}},
                "remove": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["message_ids"],
        },
    ),
    _tool(
        "gmail_list_filters",
        gmail.list_filters,
        "gmail.read",
        "List all Gmail filter rules. Each filter has id, criteria, action.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "gmail_create_filter",
        gmail.create_filter,
        "gmail.write",
        "Create a Gmail filter rule. `criteria` examples: {'from': 'noreply@github.com'}, {'subject': 'invoice', 'hasAttachment': true}, {'query': 'from:bank.com newer_than:30d'}. Actions: add_labels, remove_labels, forward_to.",
        {
            "type": "object",
            "properties": {
                "criteria": {"type": "object"},
                "add_labels": {"type": "array", "items": {"type": "string"}},
                "remove_labels": {"type": "array", "items": {"type": "string"}},
                "forward_to": {"type": "string"},
            },
            "required": ["criteria"],
        },
    ),
    _tool(
        "gmail_delete_filter",
        gmail.delete_filter,
        "gmail.write",
        "Delete a Gmail filter rule by id.",
        {"type": "object", "properties": {"filter_id": {"type": "string"}}, "required": ["filter_id"]},
    ),
    # --- Phase 7: Google Docs ---
    _tool(
        "docs_create",
        docs.create,
        "docs.write",
        "Create a new empty Google Doc. Returns {document_id, title, url}. Optional parent_folder_id moves it into a Drive folder.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "parent_folder_id": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    _tool(
        "docs_read",
        docs.read,
        "docs.read",
        "Read a Doc's title, full plain text, and heading structure. body_text is capped at 50 000 chars; _meta.body_truncated flags overflows.",
        {"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]},
    ),
    _tool(
        "docs_append_text",
        docs.append_text,
        "docs.write",
        "Append a paragraph to the end of a Doc. Optional `style` for paragraph style: h1..h6, title, subtitle, normal. Default normal.",
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "text": {"type": "string"},
                "style": {"type": "string"},
            },
            "required": ["document_id", "text"],
        },
    ),
    _tool(
        "docs_replace_text",
        docs.replace_text,
        "docs.write",
        "Find-and-replace text across the whole document. `replacements` is a dict like {'{client}': 'Иван Иванов', '{date}': '2026-05-20'}. Returns {replaced_count, per_needle}.",
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "replacements": {"type": "object"},
                "match_case": {"type": "boolean"},
            },
            "required": ["document_id", "replacements"],
        },
    ),
    _tool(
        "docs_insert_table",
        docs.insert_table,
        "docs.write",
        "Insert a (rows × cols) table. If position_index is omitted, appends at the end of the document.",
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "rows": {"type": "integer"},
                "cols": {"type": "integer"},
                "position_index": {"type": "integer"},
            },
            "required": ["document_id", "rows", "cols"],
        },
    ),
    _tool(
        "docs_export_pdf",
        docs.export_pdf,
        "docs.read",
        "Export the doc as PDF to a local path. Uses Drive's files.export under the hood.",
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "dest_path": {"type": "string"},
            },
            "required": ["document_id", "dest_path"],
        },
    ),
    # --- Phase 8: Google Slides ---
    _tool(
        "slides_create",
        slides.create,
        "slides.write",
        "Create a new empty Google Slides presentation. Returns {presentation_id, title, url, slide_count}.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "parent_folder_id": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    _tool(
        "slides_create_from_template",
        slides.create_from_template,
        "slides.write",
        "Copy a template presentation, rename copy to dest_title, replace all {placeholder} strings across every slide. The most common Slides workflow.",
        {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "replacements": {"type": "object"},
                "dest_title": {"type": "string"},
                "dest_folder_id": {"type": "string"},
            },
            "required": ["template_id", "replacements", "dest_title"],
        },
    ),
    _tool(
        "slides_read",
        slides.read,
        "slides.read",
        "Read a presentation's title + per-slide text + structure. Returns {title, slides: [{slide_id, text, object_count}], _meta}.",
        {"type": "object", "properties": {"presentation_id": {"type": "string"}}, "required": ["presentation_id"]},
    ),
    _tool(
        "slides_replace_placeholders",
        slides.replace_placeholders,
        "slides.write",
        "Find-and-replace text across every slide. `replacements` example: {'{title}': 'Q1 2026', '{client}': 'Иван'}.",
        {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "replacements": {"type": "object"},
                "match_case": {"type": "boolean"},
            },
            "required": ["presentation_id", "replacements"],
        },
    ),
    _tool(
        "slides_add_slide",
        slides.add_slide,
        "slides.write",
        "Add a new slide. layout: BLANK | TITLE | TITLE_AND_BODY | TITLE_AND_TWO_COLUMNS | SECTION_HEADER | etc. position is 0-indexed; None appends.",
        {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "layout": {"type": "string"},
                "position": {"type": "integer"},
            },
            "required": ["presentation_id"],
        },
    ),
    _tool(
        "slides_replace_image",
        slides.replace_image,
        "slides.write",
        "Replace an image (by object ID) with a new image fetched from new_url. Find object IDs via slides_read and inspecting slides[].pageElements.",
        {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "image_object_id": {"type": "string"},
                "new_url": {"type": "string"},
            },
            "required": ["presentation_id", "image_object_id", "new_url"],
        },
    ),
    _tool(
        "slides_export_pdf",
        slides.export_pdf,
        "slides.read",
        "Export the presentation as PDF to a local path. Uses Drive's files.export under the hood.",
        {
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "dest_path": {"type": "string"},
            },
            "required": ["presentation_id", "dest_path"],
        },
    ),
    # --- Phase 9: Google Forms ---
    _tool(
        "forms_create",
        forms.create,
        "forms.write",
        "Create a new Google Form. Returns {form_id, title, url, edit_url}.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "parent_folder_id": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    _tool(
        "forms_add_question",
        forms.add_question,
        "forms.write",
        "Append a question. question_type: text | paragraph | multiple_choice | checkbox | dropdown | scale | date. Choice types need `options`. Scale needs scale_low/high (and optional labels).",
        {
            "type": "object",
            "properties": {
                "form_id": {"type": "string"},
                "question_type": {"type": "string"},
                "title": {"type": "string"},
                "required": {"type": "boolean"},
                "options": {"type": "array", "items": {"type": "string"}},
                "scale_low": {"type": "integer"},
                "scale_high": {"type": "integer"},
                "scale_low_label": {"type": "string"},
                "scale_high_label": {"type": "string"},
                "paragraph": {"type": "boolean"},
            },
            "required": ["form_id", "question_type", "title"],
        },
    ),
    _tool(
        "forms_read",
        forms.read,
        "forms.read",
        "Read a form's title, description, and question list.",
        {"type": "object", "properties": {"form_id": {"type": "string"}}, "required": ["form_id"]},
    ),
    _tool(
        "forms_read_responses",
        forms.read_responses,
        "forms.read",
        "Read submissions to a form. `since` filters by RFC3339 timestamp.",
        {
            "type": "object",
            "properties": {
                "form_id": {"type": "string"},
                "since": {"type": "string"},
            },
            "required": ["form_id"],
        },
    ),
    # --- Phase 9: Google Tasks ---
    _tool(
        "tasks_list_lists",
        gtasks.list_lists,
        "tasks.read",
        "List all task lists. Each list has id + title.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "tasks_create_list",
        gtasks.create_list,
        "tasks.write",
        "Create a new task list.",
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    ),
    _tool(
        "tasks_list",
        gtasks.list_tasks,
        "tasks.read",
        "List tasks in a list. By default hides completed. Optional due_min/due_max filter by RFC3339 timestamp.",
        {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "show_completed": {"type": "boolean"},
                "due_min": {"type": "string"},
                "due_max": {"type": "string"},
            },
            "required": ["list_id"],
        },
    ),
    _tool(
        "tasks_create",
        gtasks.create,
        "tasks.write",
        "Create a new task. `due` accepts 'YYYY-MM-DD' or RFC3339.",
        {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due": {"type": "string"},
            },
            "required": ["list_id", "title"],
        },
    ),
    _tool(
        "tasks_complete",
        gtasks.complete,
        "tasks.write",
        "Mark a task as completed.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "list_id": {"type": "string"},
            },
            "required": ["task_id", "list_id"],
        },
    ),
    _tool(
        "tasks_uncomplete",
        gtasks.uncomplete,
        "tasks.write",
        "Mark a completed task back as needsAction.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "list_id": {"type": "string"},
            },
            "required": ["task_id", "list_id"],
        },
    ),
    _tool(
        "tasks_delete",
        gtasks.delete,
        "tasks.write",
        "Permanently delete a task.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "list_id": {"type": "string"},
            },
            "required": ["task_id", "list_id"],
        },
    ),
    # --- Phase 9: Google Contacts (People API) ---
    _tool(
        "contacts_search",
        contacts.search,
        "contacts.read",
        "Search user's Google Contacts. Returns flattened contact dicts with display_name, emails, phones, organizations.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    _tool(
        "contacts_get",
        contacts.get,
        "contacts.read",
        "Get full details for one contact by resource_name (e.g. 'people/c12345').",
        {"type": "object", "properties": {"resource_name": {"type": "string"}}, "required": ["resource_name"]},
    ),
    _tool(
        "contacts_list_all",
        contacts.list_all,
        "contacts.read",
        "List all contacts (capped at max_results, up to 1000). `_meta.truncated=true` if there are more.",
        {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer"},
            },
        },
    ),
    _tool(
        "contacts_create",
        contacts.create,
        "contacts.write",
        "Create a new Google Contact. Requires the `contacts` (write) scope.",
        {
            "type": "object",
            "properties": {
                "given_name": {"type": "string"},
                "family_name": {"type": "string"},
                "emails": {"type": "array", "items": {"type": "string"}},
                "phones": {"type": "array", "items": {"type": "string"}},
                "organization": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["given_name"],
        },
    ),
    _tool(
        "contacts_delete",
        contacts.delete,
        "contacts.write",
        "Permanently delete a contact by resource_name.",
        {"type": "object", "properties": {"resource_name": {"type": "string"}}, "required": ["resource_name"]},
    ),
    # --- Phase 10: external world ---
    _tool(
        "web_fetch",
        web.fetch,
        "web.read",
        "Fetch a URL. mode='text' (default, extracts visible text), 'html', or 'json'. Cap 1 MB. Returns {content, _meta:{status_code, content_type, url_final, truncated}}.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["url"],
        },
    ),
    _tool(
        "web_search",
        web.search,
        "web.read",
        "Web search via DuckDuckGo HTML (no API key). Returns {results: [{title, url, snippet}], _meta}. Best-effort — DDG HTML changes occasionally.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    _tool(
        "fx_rate",
        external.fx_rate,
        "web.read",
        "Fetch official RUB exchange rate for a currency from CBR.ru. `currency_code` is 3-letter ISO (USD, EUR, CNY...). date_iso optional (today by default). Returns {currency, date, rate_to_rub, nominal}.",
        {
            "type": "object",
            "properties": {
                "currency_code": {"type": "string"},
                "date_iso": {"type": "string", "description": "YYYY-MM-DD; defaults to today."},
            },
            "required": ["currency_code"],
        },
    ),
    _tool(
        "open_url",
        external.open_url,
        "local.write",
        "Open `url` in the user's default browser. Use for 'открой эту таблицу' / 'покажи мне в браузере'.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    ),
    _tool(
        "pdf_create",
        pdf_gen.create_pdf,
        "local.write",
        "Generate a PDF locally via reportlab. kind='text' (string content), 'table' ({headers, rows}), or 'report' ({title, sections: [{heading, paragraphs, table?}]}). Supports Cyrillic via system fonts.",
        {
            "type": "object",
            "properties": {
                "content": {},
                "dest_path": {"type": "string"},
                "kind": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["content", "dest_path"],
        },
    ),
    _tool(
        "vision_ocr",
        vision.ocr,
        "local.read",
        "OCR an image via Tesseract. `lang`='rus+eng' (default), 'rus', 'eng'. `structured=True` returns per-word bounding boxes. Requires Tesseract binary installed (see vision.py docstring).",
        {
            "type": "object",
            "properties": {
                "image_path": {"type": "string"},
                "lang": {"type": "string"},
                "structured": {"type": "boolean"},
            },
            "required": ["image_path"],
        },
    ),
    _tool(
        "vision_probe",
        vision.probe,
        "local.read",
        "Check whether Tesseract is reachable. Returns {available, info}.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "translate",
        translation.translate,
        "local.read",
        "Translate text offline via Argos Translate. First call to a new language pair downloads ~100MB. source_lang auto-detected (ru if Cyrillic, else en) if omitted.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_lang": {"type": "string"},
                "source_lang": {"type": "string"},
            },
            "required": ["text", "target_lang"],
        },
        category="translate",
    ),
    _tool(
        "translate_probe",
        translation.probe,
        "local.read",
        "Check whether Argos Translate is installed. Returns {available, info}.",
        {"type": "object", "properties": {}},
    ),
    # --- Phase 11: helpers + self-verification ---
    _tool(
        "sheets_run_formula",
        sheets.run_formula,
        "sheets.write",
        "Evaluate any Sheets formula (e.g. =GOOGLEFINANCE(\"CURRENCY:USDRUB\"), =IMPORTRANGE(...), =YEAR(TODAY())) WITHOUT creating a permanent cell. Uses temp hidden sheet — auto-cleaned. policy_op=sheets.write because temp sheet briefly mutates the file.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "formula": {"type": "string", "description": "Must start with '='. E.g. '=GOOGLEFINANCE(\"CURRENCY:USDRUB\")'."},
            },
            "required": ["spreadsheet_id", "formula"],
        },
    ),
    _tool(
        "sheets_period_detect",
        sheets.period_detect,
        "sheets.read",
        "Classify each column in the header row. Returns {periods: [{col, col_letter, label, kind}], _meta}. kind ∈ {month, quarter, year, plan_fact, other}. Use to find «какая колонка декабрь 2025» without guessing.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string"},
                "header_row": {"type": "integer", "description": "1-based row index. Default 1."},
            },
            "required": ["spreadsheet_id", "sheet"],
        },
    ),
    _tool(
        "sheets_metric_lookup",
        sheets.metric_lookup,
        "sheets.read",
        "ONE-CALL resolver: single metric+period cell. Tries named ranges → find_with_labels → period filter. Returns {value, cell, row_label, col_label}. **For aggregates (SUM/COUNT/GROUP BY/topN) use sheets_query, not this.** Ambiguity → returns candidates. For N≥5 books with the same layout: call this ONCE on a representative book to get `cell`, then `sheets_bulk_metric(rest, cell)` — never loop metric_lookup over many books.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "metric": {"type": "string", "description": "Row-label keyword: 'Чистая прибыль', 'Выручка', 'Остаток'."},
                "period": {"type": "string", "description": "Optional column-header keyword: 'Год факт', 'Декабрь 2025', 'Q1'. Omit for the LAST populated column in the row (typical YTD)."},
            },
            "required": ["spreadsheet_id", "metric"],
        },
    ),
    _tool(
        "sheets_bulk_metric",
        sheets.bulk_metric,
        "sheets.read",
        "Parallel cell-read across N spreadsheets sharing the same layout (Phase 14A). For N≥5 books, this is the right call — burns 1 API token/book at ThreadPoolExecutor(10) parallelism. Discover `cell` FIRST via `sheets_metric_lookup(representative_id, metric)` → take its `.cell` output → pass here. **No full-scan fallback** — `cell` is REQUIRED. For N>50, prefer `sheets_cross_aggregate` (1 Apps Script round-trip). Pass `dry_run=true` to see cost estimate before executing. Returns compacted {stats, outliers (top 10/bottom 10), errors (first 5), _meta.result_token}. Drill down to full per-book data via `bulk_load_results(result_token)`.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_ids": {"type": "array", "items": {"type": "string"}, "description": "List of spreadsheet IDs. Caller asserts they share layout."},
                "cell": {"type": "string", "description": "Full A1 ref, typically from metric_lookup output. E.g. 'Год факт!B45' or 'B45'."},
                "formatted": {"type": "boolean", "description": "Default false (numbers as numbers). True → string as displayed in UI."},
                "max_workers": {"type": "integer", "description": "Parallel workers, clamped to [1, 16]. Default 10."},
                "dry_run": {"type": "boolean", "description": "If true, return cost estimate without executing."},
            },
            "required": ["spreadsheet_ids", "cell"],
        },
    ),
    _tool(
        "sheets_bulk_read",
        sheets.bulk_read,
        "sheets.read",
        "Parallel read of arbitrary {spreadsheet_id, range} pairs across N books (Phase 14B). For batch reads where each cell/range can differ — e.g. «pull A1:E5 from book X AND Год факт!B45 from book Y». ThreadPoolExecutor(10) parallelism. Per-ref errors isolated. **For same-cell-across-many-books, prefer `sheets_bulk_metric`.** Returns compacted {stats over scalar values, outliers, per-ref dims}; full per-ref `values` grids spilled — retrieve via `bulk_load_results`. Pass `dry_run=true` for cost preview.",
        {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "spreadsheet_id": {"type": "string"},
                            "range": {"type": "string"},
                            "formatted": {"type": "boolean"},
                        },
                        "required": ["spreadsheet_id", "range"],
                    },
                    "description": "List of {spreadsheet_id, range, formatted?} dicts. ≥1 item.",
                },
                "formatted": {"type": "boolean", "description": "Default value mode for refs that don't override. False = raw numbers."},
                "max_workers": {"type": "integer", "description": "Parallel workers, clamped to [1, 16]. Default 10."},
                "dry_run": {"type": "boolean"},
            },
            "required": ["refs"],
        },
    ),
    _tool(
        "sheets_cross_aggregate",
        sheets.cross_aggregate,
        "sheets.read",
        "Server-side aggregation across N books via persistent Apps Script (Phase 14C). For N≥50 OR aggregates (sum/avg/min/max across many books). Chunks N into batches of `chunk_size` (default 100), runs `max_concurrent` chunks in parallel (default 5). At N=500 with defaults: 5 chunks × ~70s each running in parallel → ~70-100s total. Returns {value: <aggregate>, stats, _meta}. **Requires one-time setup** — see docs/PHASE_14_SETUP.md. First call fails with Phase14ConfigError if script_id not configured. Pass `dry_run=true` for cost preview.",
        {
            "type": "object",
            "properties": {
                "spreadsheet_ids": {"type": "array", "items": {"type": "string"}, "description": "List of spreadsheet IDs sharing layout."},
                "sheet": {"type": "string", "description": "Tab name (e.g. 'Год факт'). Must match in every book."},
                "cell": {"type": "string", "description": "A1 ref (e.g. 'B45'). Must match in every book."},
                "op": {"type": "string", "enum": ["sum", "avg", "min", "max", "count", "list"], "description": "Aggregation operation. Default 'sum'."},
                "chunk_size": {"type": "integer", "description": "Books per Apps Script call. Default 100. Each chunk takes ~60-90s. Keep ≤150 to fit under Google L7 LB timeout."},
                "max_concurrent": {"type": "integer", "description": "Parallel Apps Script invocations. Default 5."},
                "max_iterations": {"type": "integer", "description": "Per-chunk resumption cap. Default 5."},
                "dry_run": {"type": "boolean"},
            },
            "required": ["spreadsheet_ids", "sheet", "cell"],
        },
    ),
    _tool(
        "sheets_cross_aggregate_status",
        sheets.cross_aggregate_status,
        "sheets.read",
        "Peek at progress of an incomplete sheets_cross_aggregate run by its resume token. Returns {status: 'incomplete'|'not_found', processed_count, remaining_count}. Use only when cross_aggregate exhausted max_iterations and you want to see how far it got.",
        {
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Resume token from a prior incomplete cross_aggregate response."},
            },
            "required": ["token"],
        },
    ),
    _tool(
        "bulk_load_results",
        sheets.bulk_load_results,
        "sheets.read",
        "Drill down to full per-item data from a previous bulk tool result. Paginated — default limit=150 entries/page fits MAX_TOOL_PAYLOAD. For 500-book results, call with offset=0, then 150, 300, 450 until `_meta.has_more=false`. Pass the `_meta.result_token` returned by `sheets_bulk_metric` / `sheets_bulk_read`. Returns {items, errors, op, _meta:{offset, page_size, total, has_more, next_offset}}. Tokens expire after ~100 most-recent bulk results.",
        {
            "type": "object",
            "properties": {
                "result_token": {"type": "string", "description": "Token from a prior bulk tool's _meta.result_token."},
                "offset": {"type": "integer", "description": "0-based start index. Default 0."},
                "limit": {"type": "integer", "description": "Max items per page. Default 150 (≈10KB JSON for typical Drive IDs)."},
            },
            "required": ["result_token"],
        },
    ),
    _tool(
        "sheets_write_and_verify",
        sheets.write_and_verify,
        "sheets.write",
        "`write_range` + automatic verification: snapshot before + write + read-back + cell-by-cell diff. Returns {verdict: 'ok'|'modified', discrepancies}. Prefer this over `write_range` when the value must be confirmed (financial cells, agent's own writes that user will trust).",
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
        "verify_claim",
        verify.verify_claim,
        "verify.read",
        "Defensive verifier (rules 19-23). Re-reads each source NOW, returns {verdict: ok|mismatch|error, discrepancies}. source_refs: compact strings like 'sheets:SID:Год факт!B45=3087967', 'named:SID:Profit=3087967', 'drive:FID=Title', 'gmail:MSG=invoice', 'calendar:EVT=weekly' — OR dict form. Mix freely.",
        {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "What the agent is about to assert (for logging)."},
                "source_refs": {"type": "array", "items": {"type": ["object", "string"]}, "description": "Each entry: either compact 'kind:scope:loc=expected' string OR {kind, ...fields} dict."},
            },
            "required": ["claim", "source_refs"],
        },
    ),
    _tool(
        "reply_self_check",
        reply_check.self_check,
        "verify.read",
        "Lint a draft reply BEFORE emitting: detects unattributed numbers (≥4 digits without nearby Sheet!A1 / file_id / provenance hint), false-completeness claims when a recent tool was truncated, and currency tokens without cell address. Returns {ok, warnings: [{kind, span, snippet, suggestion}], _meta}. Pass `recent_meta_flags` (list of `_meta` dicts from this turn's tool calls) for the truncation check.",
        {
            "type": "object",
            "properties": {
                "draft_reply": {"type": "string"},
                "recent_meta_flags": {"type": "array", "items": {"type": "object"}, "description": "Optional. List of _meta dicts from this turn's tool results."},
            },
            "required": ["draft_reply"],
        },
    ),
    _tool(
        "self_run_tests",
        self_heal.self_run_tests,
        "self.test",
        "Run pytest on a pattern (default 'tests/test_*.py'). Beyond self_smoke_test (imports only), this exercises actual test cases. Use after self_edit before self_git_commit.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "deselect": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    _tool(
        "self_list_tools",
        self_heal.self_list_tools,
        "self.read",
        "Introspect every registered tool. Returns {tools: [{name, policy_op, description, has_account_param}], _meta}. Useful for the agent to self-orient on its own capabilities.",
        {"type": "object", "properties": {}},
    ),
]


BY_NAME = {t["name"]: t for t in TOOLS}
POLICY_OP_BY_TOOL = {t["name"]: t["policy_op"] for t in TOOLS}


def list_categories() -> dict:
    """Return `{category: [tool_names]}` for the entire TOOLS registry.

    Categories come from each spec's `category` field (default: name prefix
    before first underscore). Useful for introspection and the opt-in
    dynamic filtering via `select_tools()`.
    """
    out: dict[str, list[str]] = {}
    for t in TOOLS:
        cat = t.get("category", "misc")
        out.setdefault(cat, []).append(t["name"])
    return out


def select_tools(categories) -> list:
    """Return TOOLS specs whose category is in `categories` (str OR list[str]).

    Use to construct a reduced tool set for one turn — pass to whatever
    consumer expects a list of specs. Order preserved from registration.
    """
    if isinstance(categories, str):
        wanted = {categories}
    else:
        wanted = set(categories or [])
    return [t for t in TOOLS if t.get("category") in wanted]


# Max chars sent back to the model per tool call. ~20k chars ≈ 6-7k tokens
# (Russian text is ~2-3 chars/token), well under the 25k-token internal
# budget Anthropic recommends for tool responses ("Writing effective tools
# for agents", Sep 11 2025). Outputs above this get wrapped in a STRUCTURED
# truncation envelope (`_truncated=true` + `_meta`) so the agent can still
# json.parse the result and see `full_payload_chars` + a tool-specific hint.
MAX_TOOL_PAYLOAD = 20000


def _truncate_payload(payload: str, name: str, warn_prefix: str | None) -> str:
    """Replace an over-budget payload with a STRUCTURED truncation envelope
    the agent can still json.parse. Embeds the first N chars of the original
    output as `preview` plus `_meta` with full-size, hint, and the
    `_truncated=true` marker."""
    full_chars = len(payload)
    body_for_preview = payload[len(warn_prefix):] if warn_prefix else payload
    # Reserve ~500 chars for the envelope itself
    preview_budget = MAX_TOOL_PAYLOAD - 500
    preview = body_for_preview[:preview_budget]
    envelope = {
        "_truncated": True,
        "preview": preview,
        "_meta": {
            "tool": name,
            "full_payload_chars": full_chars,
            "shown_chars": len(preview),
            "truncated_by_payload": True,
            "hint": _truncation_hint(name),
        },
    }
    out = json.dumps(envelope, ensure_ascii=False)
    if warn_prefix:
        out = warn_prefix + out
    return out


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


from src.tools._errors import _IdempotencyConflict, _classify_exception, _classify_http_error

_RETRYABLE_KINDS = {"rate_limit", "server", "network"}


# ---------- RFC 9457 problem+json error envelope ----------
# RFC 9457 (replaces 7807) defines a standard structure for HTTP error
# payloads: {type, title, status, detail, instance} plus arbitrary extensions.
# We use it at the tool-error boundary so the agent (and any future MCP
# client) sees a uniform, machine-actionable shape across all 237 tools.
#
# Extensions: error_kind, retriable, retry_after_ms, fix_hint, exception_type.
# Legacy `error` + `_meta` keys are kept for backward compatibility with
# existing tests and system-prompt rule 23 references.

_PROBLEM_TITLES: dict[str, tuple[str, str]] = {
    # error_kind → (title, fix_hint)
    "auth_scope": (
        "Insufficient OAuth scope",
        "Re-OAuth this account with the scopes the tool needs (see /accounts UI or `uv run python -m src.cli add <alias>`).",
    ),
    "permission": (
        "Permission denied",
        "Token is valid but lacks IAM/ACL access. Check sharing or workspace policy on the resource.",
    ),
    "not_found": (
        "Resource not found",
        "Verify the ID or path; the target may be deleted or never existed.",
    ),
    "bad_input": (
        "Invalid request",
        "Check the parameter values against the tool's schema — likely a malformed range, body, or ID.",
    ),
    "rate_limit": (
        "Rate limited by upstream",
        "Back off and retry after `retry_after_ms`; consider batching or `verify_claim` parallel mode.",
    ),
    "server": (
        "Upstream server error",
        "Transient — retry with exponential backoff (Google formula: min(2^n*1000+rand_ms, 64000)).",
    ),
    "network": (
        "Network error",
        "Transient — retry. If persistent, check connectivity to the upstream service.",
    ),
    "idempotency_conflict": (
        "Idempotency key reused with different args",
        "Pick a fresh `idempotency_key`, or call again with identical args.",
    ),
    "unknown": (
        "Unexpected error",
        "Inspect `detail` and `exception_type`; if reproducible, treat as a bug.",
    ),
}

# Default retry-after for rate_limit when the upstream didn't include a header.
# Google's recommended backoff: min(2^n*1000+rand_ms, 64000). First retry n=1 → ~2s.
_DEFAULT_RATE_LIMIT_RETRY_MS = 2000


def _build_problem(
    e: Exception,
    tool_name: str,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an RFC 9457-shaped problem dict for an exception. `tool_name`
    becomes `instance`; `extras` is merged at top level (e.g. for the
    idempotency-conflict path that builds a synthetic problem)."""
    kind, status = _classify_exception(e)
    title, fix_hint = _PROBLEM_TITLES.get(kind, _PROBLEM_TITLES["unknown"])
    retriable = kind in _RETRYABLE_KINDS
    retry_after_ms: int | None = None
    if kind == "rate_limit":
        retry_after_ms = _DEFAULT_RATE_LIMIT_RETRY_MS
    detail = f"{type(e).__name__}: {e}"[:600]
    problem: dict[str, Any] = {
        # ---- RFC 9457 canonical fields ----
        "type": f"about:blank#{kind}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": tool_name,
        # ---- extensions (allowed by RFC 9457 §3.2) ----
        "error_kind": kind,
        "retriable": retriable,
        "retry_after_ms": retry_after_ms,
        "fix_hint": fix_hint,
        "exception_type": type(e).__name__,
        "_format": "application/problem+json",
        # ---- legacy aliases (will be retired once system prompt updates) ----
        "error": detail,
        "_meta": {
            "error_kind": kind,
            "http_status": status,
            "retryable": retriable,
            "exception_type": type(e).__name__,
        },
    }
    if extras:
        problem.update(extras)
    return problem


def _meta_warning_prefix(result) -> str | None:
    """Build a one-line ⚠️ prefix from `_meta` fields the agent must NOT miss.

    Agents tend to read `values`/`rows` and skip `_meta`. This visible
    prefix surfaces critical flags (truncation, empty-not-because-of-data,
    silent default windows, semantic→substring fallback) on top of the
    JSON payload — so they're impossible to ignore on a quick scan.

    Returns None when nothing demands attention; caller skips prefixing.
    """
    if not isinstance(result, dict):
        return None
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        return None
    flags: list[str] = []
    if meta.get("truncated"):
        reason = meta.get("truncation_reason") or "results clipped"
        flags.append(f"truncated ({str(reason)[:80]})")
    er = meta.get("empty_reason")
    if er and er not in {"no_data"}:
        flags.append(f"empty_reason={er}")
    # Calendar's window.default_used (nested) — and a flat default_used variant
    if meta.get("default_used"):
        flags.append("default_window_used")
    window = meta.get("window")
    if isinstance(window, dict) and window.get("default_used"):
        flags.append("default_window_used")
    if meta.get("search_method") == "substring":
        flags.append("semantic_fell_back_to_substring")
    if not flags:
        return None
    # Deduplicate while preserving order — flat + nested could both trigger default_window_used
    seen: set[str] = set()
    deduped = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return "⚠️ META: " + "; ".join(deduped) + "\n\n"


_POLICY_OP_TO_BUCKET = {
    "sheets": "sheets-direct",
    "drive": "drive",
    "gmail": "gmail",
    "apps_script": "apps-script",
}


def _bucket_for_policy_op(policy_op: str | None) -> str | None:
    """Map a tool's policy_op prefix to a quota bucket.

    Returns None for tools we don't budget (calendar, docs, slides, forms,
    contacts, self.*, verify.*, etc. — these don't share Google's hot
    user-quota path or are bookkeeping calls).
    """
    if not policy_op:
        return None
    prefix = policy_op.split(".", 1)[0]
    return _POLICY_OP_TO_BUCKET.get(prefix)


def _wrap_for_sdk(spec):
    """Decorate a sync tool fn as an async @tool that returns SDK-compatible output.

    On success, prepends a `⚠️ META: …` prefix when `_meta` signals demand
    attention (truncation, abnormal empty, default window used, semantic
    fallback) — so the agent can't skip past them.

    On exception, classifies and returns a structured error payload with
    `_meta.error_kind` for recovery strategy (see system prompt rule 23).

    Phase 14F: budgets each call against a per-service sliding window
    (sheets-direct / drive / gmail / apps-script). When pacing kicks in,
    surfaces `_meta.quota_paced_ms` so the agent learns it's quota-bound.
    """
    name = spec["name"]
    fn = spec["fn"]
    description = spec["schema"]["description"]
    input_schema = spec["schema"]["input_schema"]
    bucket = _bucket_for_policy_op(spec.get("policy_op"))
    annotations = spec.get("annotations")
    supports_idempotency = spec.get("supports_idempotency", False)
    supports_dry_run = spec.get("supports_dry_run", False)
    native_dry_run = spec.get("native_dry_run", False)

    @tool(name, description, input_schema, annotations=annotations)
    async def wrapped(args):
        # Optional OTel span — no-op when opentelemetry-api isn't installed.
        # Each tool call becomes a span with name `tool.<name>` so traces
        # show end-to-end agent → tool → upstream API behaviour in Langfuse
        # / Phoenix / Jaeger.
        #
        # `start_as_current_span` returns a context manager; `__enter__`
        # yields the Span itself (mutable — set_attribute, record_exception,
        # set_status). We keep both: `_otel_cm` to close on exit, `_otel_span`
        # to populate as the call progresses.
        _otel_cm = None
        _otel_span = None
        if _otel_trace is not None:
            try:
                _otel_cm = _otel_trace.get_tracer("workspace_agent").start_as_current_span(f"tool.{name}")
                _otel_span = _otel_cm.__enter__()
                _otel_span.set_attribute("tool.name", name)
                try:
                    _otel_span.set_attribute("tool.tenant_id", _current_tenant_id())
                except Exception:
                    pass
            except Exception:
                _otel_cm = None
                _otel_span = None
        _started = asyncio.get_event_loop().time()
        # Dry-run gate. If the tool's destructive AND the caller asked for a
        # preview, either pass through (native impl) or return a stub
        # envelope. Stub means: "your call was accepted, here's what would
        # have been sent, but nothing was executed."
        dry_run_requested = False
        if supports_dry_run:
            if native_dry_run:
                # Native impl wants the kwarg — leave it in args.
                dry_run_requested = bool(args.get("dry_run", False))
            else:
                # No native impl — strip from args and stub here.
                dry_run_requested = bool(args.pop("dry_run", False))
        if _otel_span is not None:
            try:
                _otel_span.set_attribute("tool.dry_run", bool(dry_run_requested))
            except Exception:
                pass
        if supports_dry_run and not native_dry_run and dry_run_requested:
            stub = {
                "dry_run": True,
                "executed": False,
                "tool": name,
                "args": args,
                "plan": {
                    "would_call": name,
                    "with_args": args,
                    "note": (
                        f"`{name}` does not yet implement a native dry_run "
                        "preview. Stub returned: nothing was executed. "
                        "Re-call without `dry_run` to perform the operation."
                    ),
                },
                "_meta": {"native_preview": False},
            }
            return {"content": [{"type": "text", "text": json.dumps(stub, ensure_ascii=False)}]}

        # Stripe-style idempotency. Only enabled when (a) the tool was tagged
        # non-idempotent at registration, and (b) the caller supplied a key.
        idempotency_key = args.pop("idempotency_key", None) if supports_idempotency else None
        if _otel_span is not None:
            try:
                _otel_span.set_attribute("tool.idempotency_key_present", bool(idempotency_key))
            except Exception:
                pass
        if idempotency_key:
            cached = await asyncio.to_thread(_idempotency.lookup, idempotency_key, name, args)
            if cached.get("hit"):
                if cached.get("mismatch"):
                    # Synthetic exception just to reuse _build_problem's
                    # title/fix_hint mapping for idempotency_conflict.
                    conflict_msg = (
                        f"idempotency_key {idempotency_key!r} was already used "
                        f"with different args for tool {name!r}."
                    )
                    problem = _build_problem(
                        _IdempotencyConflict(conflict_msg),
                        name,
                        extras={"key_age_seconds": cached.get("age_seconds")},
                    )
                    payload = json.dumps(problem, ensure_ascii=False)
                    return {"content": [{"type": "text", "text": payload}], "is_error": True}
                # Replay cached response verbatim — DO NOT re-execute the tool.
                return cached["response"]

        # Phase 14F: proactive quota pacing. acquire() is fast when window
        # has capacity; sleeps just long enough to clear when full. No-op for
        # tools without a configured bucket.
        paced_ms = 0.0
        if bucket:
            paced_ms = await asyncio.to_thread(_quota.acquire, bucket)
        # Track outcome so the `finally` block can emit a single metrics
        # record. Mutated by the success branch + the exception branch.
        outcome: dict[str, Any] = {"ok": False, "error_kind": None}
        try:
            result = await asyncio.to_thread(fn, **args)
            if result is None:
                outcome["ok"] = True
                return {"content": [{"type": "text", "text": "(no output)"}]}
            # Surface quota signal on existing _meta dict (don't synthesize
            # one — many tools deliberately return non-dict results)
            if bucket and isinstance(result, dict):
                meta = result.get("_meta")
                if isinstance(meta, dict):
                    if paced_ms > 0:
                        meta["quota_paced_ms"] = paced_ms
                    rem = _quota.remaining_pct(bucket)
                    if rem is not None:
                        meta["quota_remaining_pct"] = round(rem, 3)
            payload = json.dumps(result, default=str, ensure_ascii=False)
            warn = _meta_warning_prefix(result)
            if warn:
                payload = warn + payload
            if len(payload) > MAX_TOOL_PAYLOAD:
                payload = _truncate_payload(payload, name, warn)
            response = {"content": [{"type": "text", "text": payload}]}
            # Cache successful responses for replay on retry with same key.
            # Errors are NOT cached — we want the next retry to re-attempt.
            if idempotency_key:
                await asyncio.to_thread(_idempotency.store, idempotency_key, name, args, response)
            outcome["ok"] = True
            return response
        except Exception as e:
            problem = _build_problem(e, name)
            outcome["error_kind"] = (problem.get("_meta") or {}).get("error_kind")
            if _otel_span is not None:
                try:
                    _otel_span.record_exception(e)
                    if _OtelStatus is not None and _OtelStatusCode is not None:
                        _otel_span.set_status(_OtelStatus(_OtelStatusCode.ERROR, str(e)[:200]))
                except Exception:
                    pass
            payload = json.dumps(problem, ensure_ascii=False)
            return {
                "content": [{"type": "text", "text": payload}],
                "is_error": True,
            }
        finally:
            latency_ms = (asyncio.get_event_loop().time() - _started) * 1000.0
            if _otel_span is not None:
                try:
                    _otel_span.set_attribute("tool.status", "ok" if outcome["ok"] else "error")
                    _otel_span.set_attribute("tool.latency_ms", latency_ms)
                    if outcome["error_kind"]:
                        _otel_span.set_attribute("tool.error_kind", outcome["error_kind"])
                    if paced_ms > 0:
                        _otel_span.set_attribute("tool.quota_paced_ms", paced_ms)
                except Exception:
                    pass
            if _otel_cm is not None:
                try:
                    _otel_cm.__exit__(None, None, None)
                except Exception:
                    pass
            # Record metrics regardless of outcome. Latency is wall-clock
            # ms from the start of the wrapped call (covers quota pacing
            # + idempotency lookup + the actual tool body).
            try:
                _metrics.record_tool_call(
                    name, latency_ms,
                    ok=outcome["ok"], error_kind=outcome["error_kind"],
                )
            except Exception:
                # Metrics must never break a tool call.
                pass
    return wrapped


def build_sdk_mcp_server():
    """Construct the in-process SDK MCP server with all 28 tools."""
    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version="1.0.0",
        tools=[_wrap_for_sdk(t) for t in TOOLS],
    )
