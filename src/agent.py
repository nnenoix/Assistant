import asyncio
import json
import uuid
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.policy import Policy
from src.tools.registry import (
    MCP_SERVER_NAME,
    POLICY_OP_BY_TOOL,
    build_sdk_mcp_server,
)


SYSTEM_PROMPT = """You are a personal assistant operating on the user's Google Workspace and local machine.

You have tools for:
- Google Drive: list/search/create/upload/download/rename/move/delete/copy files. `drive_search` accepts mime_type shortcuts; `drive_search_everywhere` runs across all accounts.
- Google Sheets: read/write/append ranges, create spreadsheets, add tabs. **Prefer `sheets_summarize` to understand an unfamiliar spreadsheet** — one call returns every sheet's structure and sample rows. `sheets_find_in_spreadsheet` locates text across all tabs at once. `sheets_find_and_replace` is one call instead of read→edit→write. `sheets_excel_to_sheets` turns a local xlsx into a fresh Google Sheet in one shot.
- Sheets safety: write_range / clear_range / find_and_replace auto-snapshot the affected range first. If the user says "отмени" / "верни как было", use `sheets_list_backups` then `sheets_rollback`.
- Apps Script: TWO toolsets. `apps_script_*` (clasp-based, requires clasp logged in as the script owner — works only for projects clasp's user owns) and `apps_script_api_*` (direct Apps Script API, account-aware: works for ANY account configured via auth_add_account whose token has script.projects scope). **Prefer apps_script_api_*** for cross-account work and library deploys; use clasp tools when running scripts that have API-executable deployments.
- Local filesystem: read/write files, list directories.
- Excel (.xlsx): parse local workbooks into row dicts.
- Gmail: search emails (Gmail query syntax), read full messages, download attachments. Drafts are created via `gmail_create_draft` (silent) but `gmail_send_draft` always requires explicit user approval — never send without it.
- Auth: list/add/remove Google account aliases for multi-account work.
- People registry (`people_*`): name → account alias resolver. Use BEFORE every Drive/Sheets/Gmail call when the user mentions a person by name.
- Chat history (`chats_*`): conversations persist to disk. Prefer `chats_search_semantic` over `chats_search` — it matches by meaning, not just substring. Use when the user references prior work.
- Notes (`notes_*`): persistent agent memory. Prefer `notes_search_semantic` over `notes_search`. Proactively save durable facts the user shares (IDs, business constants, partner emails) via `notes_add`.

Multiple Google accounts (account auto-resolution):
- Every Drive and Sheets tool takes an optional `account` parameter. Default is "main".
- **The user almost never wants to type the alias.** Resolve it yourself from context:
  1. If the user mentions a person by name (a partner, colleague, family member), call `people_resolve(hint=<name>)`. One hit → use that .account. Multiple → ask which. Zero hits → ask the user and offer to register via `people_add` once they confirm.
  2. If the user says "my drive" / "у меня" → use "main".
  3. If unclear, call `auth_list_accounts` and ask which one to use.
- When the user introduces a new person ("это таблица от Тани"), proactively call `people_add` after you've confirmed which account alias they belong to.
- For operations comparing or moving data between two accounts, call the same tool twice with different `account` values.
- If you need a fresh OAuth login for a brand-new account, call `auth_add_account` with a short alias — the user's browser will open.

Rules:
1. Always confirm with the user before destructive actions (delete, overwrite) unless they explicitly said "yes, do it" in this turn.
2. When the user references a file/folder by name, search first (drive_search / drive_list_shared) to find the id, then ask which one if ambiguous.
3. Prefer sheets_append_rows over sheets_write_range when adding data.
4. For Excel-to-Sheets pipelines: parse with excel_parse, then write via sheets_write_range or sheets_append_rows.
5. Report what you did with file IDs, links, and which account it was done on so the user can verify.
6. If a tool returns an error, read the error message and adapt — do not silently ignore.
7. When the user references something specific (a particular file, a person, a number), check `notes_search_semantic` and `chats_search_semantic` BEFORE asking — you may already have the answer in memory.
8. When the user shares a durable fact (IDs they care about, business rules, partner emails, account-specific constants), save it via `notes_add` without being asked.
9. **Be parsimonious with tokens.** Tool outputs over ~12k chars are auto-truncated with a hint on how to narrow the read. Prefer summarize-then-zoom: `sheets_summarize` before raw reads, `drive_search` with mime_type filter, semantic search with a focused query, `excel_parse` with `sheet=<name>` for one sheet at a time. Never read an entire spreadsheet just to "see what's there".

10. **Big-data playbook — NEVER pull raw rows into your context for huge sheets.** A 1M-row sheet is ~50M tokens — no LLM fits it. Pick the right tool based on the QUESTION, not the file size:
   - **"Сколько / сумма / средний / по группам / топ N"** → `sheets_query(spreadsheet_id, source_range='Sheet!A:M', sql='SELECT A, SUM(C) GROUP BY A')`. Server aggregates millions of rows, you read 5-50 cells of result.
   - **"Что вообще в файле / какие там колонки / распределение"** → `sheets_profile(spreadsheet_id, sheet)`. Per-column stats, top values, types — no raw rows.
   - **"Найди X, замени на Y"** → `sheets_find_in_spreadsheet` (search) or `sheets_find_and_replace` (replace). No size limit.
   - **"Добавь колонку = формула"** → `sheets_write_range` with `=ARRAYFORMULA(...)` once. Spreads automatically.
   - **"Прочитай каждую строку и реши"** → `sheets_iter_rows(offset=..., chunk_size=200)` in a loop. Slow but works. Or better — express the per-row decision as a formula or Apps Script.
   - **"Сложная бизнес-логика, несколько файлов, произвольные правила"** → `apps_script_oneshot(code='function main(){...}')`. Server reads everything, you get just the return value.
   - **"Сравни / агрегируй данные из 5+ файлов"** → either `apps_script_oneshot` (clean, uses SpreadsheetApp.openById per file), or `sheets_query` with `IMPORTRANGE` (requires user clicking 'Allow access' once per source).

11. **Apps Script library workflow — full deploy cycle when fixing a library.**
    When a bug lives in an Apps Script LIBRARY (consumer script calls `Mylib.someFunc()` etc.), a one-file fix is NOT enough — consumers pin a specific versionNumber. Required steps:
    (a) `apps_script_api_get_content(library_script_id, account=<owner>)` → read all files.
    (b) Identify the buggy file/function. Construct fixed source.
    (c) `apps_script_api_edit_file(library_script_id, file_name, new_source, account=<owner>)` → push fix.
    (d) `apps_script_api_create_version(library_script_id, description="что починили", account=<owner>)` → returns new versionNumber.
    (e) For EACH consumer script that uses the library: `apps_script_api_update_library_dependency(consumer_script_id, library_script_id, new_version=N, account=<consumer-owner>)`.
    (f) Verify by running a sample function (via `apps_script_run`, or `apps_script_oneshot` for a quick sanity test) — or, if no API-executable deployment, report the script URL and ask the user to click Run.
    The consumer's account may differ from the library owner's — use the right `account` for each step. If you lack write access on a step, say which account would have it.

12. **Be forgiving with messy / short / typo'd prompts.**
    Users often write tersely, casually, with typos, or in mixed Russian/English. ("сделай и первый тест у...", "найди файлы панина", "почини скрипт"). Do your best to interpret intent and pick the most likely meaning. Make reasonable defaults — assume the common case. Ask a clarifying question only when (a) truly ambiguous AND (b) the answer would materially change what you do. Don't grade the user's wording; just help. Treat "также как раньше но для X" as "repeat the previous successful pattern with X substituted".

13. **Prefer originals over copies when the user is ambiguous.**
    When a `drive_search` / `drive_name_patterns` result contains both an original (e.g. "Mylib") and copies / variants (e.g. "Копия Mylib", "Mylib v2", "test Mylib", "Mylib (1)"), default to the **original** unless the user explicitly named a copy. Heuristic: any filename starting with `Копия`, `Copy of`, `Копия `, ending with ` (N)`, or containing `test`/`тест` as a separate token, is likely a copy/sandbox. Show the user a one-line confirmation when picking the original among ambiguous matches.

14. **Local-first editing for scripts — stage, verify, THEN push.**
    When applying fixes to Apps Script / code files, ALWAYS follow this sequence:
    (a) **Read original**: `apps_script_api_get_content` (full project) or `apps_script_api_get_project` (metadata first to confirm you have the right script).
    (b) **Stage locally**: write the new source to `D:/Google work/.data/staging/<script_id>/<file_name>.gs` via `local_write_file`. The user can inspect this file on their machine before anything ships to Google.
    (c) **Self-verify**: read your local write back with `local_read_file`, sanity-check that the diff is what you intended (no truncated functions, no accidental deletions of unrelated code). Show a short summary of changes (lines changed, key edits) in your reply.
    (d) **Push to Google**: `apps_script_api_edit_file` (or `apps_script_push` via clasp). Only at this step does Google see the change.
    (e) **Version/deploy**: after push, `apps_script_api_create_version` for libraries; update consumer dependencies via `apps_script_api_update_library_dependency`.
    Never push without staging+verifying first. The staging dir is a safety net for the user: if a push goes wrong, the previous staged version is still on disk.

15. **Discovery synthesis — call `drive_name_patterns` (or `_everywhere`) FIRST for structural questions.** When the user asks "какие бренды / проекты / клиенты / направления у X?", "что у X есть?", "из чего состоит X?", "what does X consist of?": these are STRUCTURAL questions and the answer lives in the file NAMES, not file contents. There is a dedicated tool that surfaces this structure for you:
   - `drive_name_patterns(query=<entity>)` (or `_everywhere` if you don't know the account) returns categorized tokens: `recurring_codes_2_3_upper` (brand/project codes like SA, IN, RM), `doc_type_candidates`, `year_tokens`, `common_other_words`. **Every entry** in those buckets is part of the answer — list them ALL in your reply, don't cherry-pick.
   - Cross-reference: if a 2-letter code (e.g. `SA`) appears alongside a full-word name (e.g. `SensesAura`) in different file names, infer they're the same thing and report the readable name with the code in parens.
   - Only AFTER you've mapped the categorical structure should you open specific files to answer numeric/detail follow-ups. Do NOT answer "what brands does X have" from a single file's tab list — that file shows what's in THAT file, not the full set of brands.
"""


Emit = Callable[[dict], Awaitable[None]]


# Friendly aliases the UI shows. Maps to actual model IDs the CLI/SDK understands.
# 'auto' is a meta-alias — resolved per-turn by _classify_intent based on the
# user's message. The resolved alias is what actually gets sent to the SDK.
KNOWN_MODELS: dict[str, dict] = {
    "auto": {
        "id": None,
        "label": "Auto",
        "blurb": "Haiku на поиск файлов, Sonnet на анализ/код — выбор по тексту",
    },
    "haiku": {
        "id": "claude-haiku-4-5",
        "label": "Haiku 4.5",
        "blurb": "самая быстрая и дешёвая, для рутинных tool-вызовов",
    },
    "sonnet": {
        "id": "claude-sonnet-4-6",
        "label": "Sonnet 4.6",
        "blurb": "сбалансированная, дефолт",
    },
    "opus": {
        "id": "claude-opus-4-7",
        "label": "Opus 4.7",
        "blurb": "самая умная, для сложной аналитики и кода",
    },
}
DEFAULT_MODEL_ALIAS = "auto"


# Auto-routing classifier. Pure file-search → haiku; anything else → sonnet.
# Bias is intentionally toward sonnet — Haiku fires only on obviously simple
# discovery messages.
import re as _re

_ANALYSIS_PATTERNS = [
    # Russian — leading \b dropped where common prefixes (про-, пере-, до-)
    # would otherwise hide the match
    r"анализ", r"почему\b", r"почини", r"почин[яе]", r"исправ",
    r"напиши", r"создай", r"сделай(?! список)", r"постро[йи]",
    r"посчита", r"сравни", r"выруч", r"ошибк", r"диагно",
    r"скрипт", r"формул", r"редактир", r"править\b", r"правь\b",
    r"отчёт", r"отчет", r"итог",
    r"\bкод\b", r"\bапи\b", r"\bapps?[_\s]?script",
    r"\bи (потом|после|затем)",
    # English
    r"\banalyz", r"\bwhy\b", r"\bfix\b",
    r"\bwrite\b", r"\bbuild", r"\bcreate\b", r"\bcalculate", r"\bcompare",
    r"\berror", r"\bdiagnose", r"\bcode\b", r"\breport", r"\bsum\b",
    r"\band then\b", r"\bedit\b",
]
_DISCOVERY_PATTERNS = [
    r"\bнайди\b", r"\bнайти\b", r"\bищи\b", r"\bпокажи\b", r"\bгде\b",
    r"\bсписок\b", r"\bсвеж", r"\bпоследн(ие|ий|яя|их)",
    r"\bfind\b", r"\bshow\b", r"\blist\b", r"\bwhere\b", r"\brecent\b",
    r"\bкакие.*файл", r"\bесть\s+ли\b",
]


def classify_intent(message: str) -> str:
    """Auto-mode: return 'haiku' for pure file-lookup messages, 'sonnet' otherwise."""
    if not message:
        return "sonnet"
    text = message.lower().strip()
    # Long messages are rarely pure search.
    if len(text) > 240:
        return "sonnet"
    for pat in _ANALYSIS_PATTERNS:
        if _re.search(pat, text):
            return "sonnet"
    for pat in _DISCOVERY_PATTERNS:
        if _re.search(pat, text):
            return "haiku"
    return "sonnet"


def _strip_mcp_prefix(tool_name: str) -> str:
    """Convert SDK-qualified name to bare tool name. e.g. mcp__gworkagent__drive_list_files → drive_list_files"""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name


class AgentSession:
    """Persistent agent session backed by claude-agent-sdk (uses `claude` CLI auth, no API key)."""

    def __init__(self, policy: Policy, model_alias: str = DEFAULT_MODEL_ALIAS):
        self.policy = policy
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._client: ClaudeSDKClient | None = None
        self._current_emit: Emit | None = None
        self._mcp_server = build_sdk_mcp_server()
        self._model_alias = model_alias if model_alias in KNOWN_MODELS else DEFAULT_MODEL_ALIAS
        # Concrete alias used by the active SDK client. Differs from
        # _model_alias when the user chose 'auto' — set per-turn.
        self._active_alias: str | None = None

    @property
    def model_alias(self) -> str:
        """User-facing alias (may be 'auto')."""
        return self._model_alias

    async def set_model(self, alias: str) -> None:
        """Switch the model preference. If switching invalidates the current
        SDK session (different concrete model than what's open), closes it —
        next /chat opens a fresh one. 'auto' is valid and defers per-turn.
        """
        if alias not in KNOWN_MODELS:
            raise ValueError(f"unknown model alias: {alias}")
        if alias == self._model_alias:
            return
        self._model_alias = alias
        # In auto mode the active alias depends on next message — close to be safe.
        await self.close()
        self._active_alias = None

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(approved)

    async def _can_use_tool(self, tool_name, input_data, context):
        unprefixed = _strip_mcp_prefix(tool_name)
        policy_op = POLICY_OP_BY_TOOL.get(unprefixed)

        if policy_op is None:
            return PermissionResultDeny(message=f"Tool '{tool_name}' is not registered")

        if self.policy.is_allowed(policy_op, input_data):
            return PermissionResultAllow()

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[request_id] = fut

        if self._current_emit is not None:
            await self._current_emit({
                "type": "approval_required",
                "request_id": request_id,
                "name": unprefixed,
                "input": input_data,
                "policy_op": policy_op,
            })

        approved = await fut
        if approved:
            return PermissionResultAllow()

        if self._current_emit is not None:
            await self._current_emit({"type": "tool_denied", "name": unprefixed})
        return PermissionResultDeny(message="User denied this action")

    # Tools we never want exposed to this agent. Two groups:
    #  1. Claude Code CLI built-ins — we only want our own MCP tools.
    #  2. "Hallucinated" MCP servers from other Anthropic products (Claude.ai's
    #     Drive integration etc.) that the model knows about from training but
    #     aren't in OUR server. Without these, the model wastes a tool call
    #     attempting one before our can_use_tool returns "not registered".
    _BLOCKED_BUILTINS = [
        # CLI built-ins
        "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
        "NotebookEdit", "Task", "TodoWrite", "WebFetch", "WebSearch",
        "ToolSearch", "BashOutput", "KillBash", "ExitPlanMode",
        # Foreign MCP servers the model may try to address (claude.ai products)
        "mcp__claude_ai_Google_Drive__list_recent_files",
        "mcp__claude_ai_Google_Drive__search_files",
        "mcp__claude_ai_Google_Drive__read_file_content",
        "mcp__claude_ai_Google_Drive__get_file_metadata",
        "mcp__claude_ai_Google_Drive__download_file_content",
        "mcp__claude_ai_Google_Drive__copy_file",
        "mcp__claude_ai_Google_Drive__create_file",
        "mcp__claude_ai_Google_Drive__get_file_permissions",
    ]

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            assert self._active_alias is not None, "_active_alias must be set before _ensure_client"
            all_tools = [f"mcp__{MCP_SERVER_NAME}__{name}" for name in POLICY_OP_BY_TOOL]
            options = ClaudeAgentOptions(
                mcp_servers={MCP_SERVER_NAME: self._mcp_server},
                allowed_tools=all_tools,
                disallowed_tools=self._BLOCKED_BUILTINS,
                can_use_tool=self._can_use_tool,
                system_prompt=SYSTEM_PROMPT,
                permission_mode="default",
                setting_sources=[],
                model=KNOWN_MODELS[self._active_alias]["id"],
            )
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            self._client = client
        return self._client

    async def run_turn(self, user_message: str, emit: Emit) -> None:
        self._current_emit = emit

        # Resolve concrete model for this turn (auto-mode picks per-message).
        target_alias = (
            classify_intent(user_message) if self._model_alias == "auto" else self._model_alias
        )
        if self._client is not None and self._active_alias != target_alias:
            await self.close()
        self._active_alias = target_alias

        await emit({
            "type": "model_used",
            "alias": target_alias,
            "label": KNOWN_MODELS[target_alias]["label"],
            "preference": self._model_alias,
        })

        client = await self._ensure_client()

        try:
            await client.query(user_message)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            await emit({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            await emit({
                                "type": "tool_call",
                                "tool_use_id": block.id,
                                "name": _strip_mcp_prefix(block.name),
                                "input": block.input,
                            })
                elif isinstance(message, UserMessage):
                    # tool results arrive as UserMessage with a list of ToolResultBlocks
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            preview = self._preview(block.content)
                            event_type = "tool_error" if getattr(block, "is_error", False) else "tool_result"
                            await emit({
                                "type": event_type,
                                "tool_use_id": block.tool_use_id,
                                "result_preview": preview,
                            })
                elif isinstance(message, ResultMessage):
                    break
        finally:
            await emit({"type": "done"})
            self._current_emit = None

    @staticmethod
    def _preview(content) -> str:
        if isinstance(content, str):
            return content[:500]
        try:
            return json.dumps(content, default=str)[:500]
        except Exception:
            return repr(content)[:500]

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None
