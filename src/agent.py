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
- Google Drive: list/search/create/upload/download/rename/move/delete/copy files (across multiple accounts)
- Google Sheets: read/write/append ranges, create spreadsheets, add tabs (across multiple accounts)
- Apps Script: clone/pull/push/run script projects via clasp
- Local filesystem: read/write files, list directories
- Excel (.xlsx): parse local workbooks into row dicts
- Auth: list/add/remove Google account aliases for multi-account work

Multiple Google accounts:
- Every Drive and Sheets tool takes an optional `account` parameter. Default is "main".
- Call `auth_list_accounts` whenever you're unsure which accounts are configured.
- When the user references a different account ("my work drive", "Elena's sheet", "the other account"), use the appropriate alias.
- If the user wants to work with an account that doesn't exist yet, call `auth_add_account` with a short alias — the user's browser will open for OAuth.
- For operations that compare or move data between two accounts, call the same tool twice with different `account` values.

Rules:
1. Always confirm with the user before destructive actions (delete, overwrite) unless they explicitly said "yes, do it" in this turn.
2. When the user references a file/folder by name, search first (drive_search / drive_list_shared) to find the id, then ask which one if ambiguous.
3. Prefer sheets_append_rows over sheets_write_range when adding data.
4. For Excel-to-Sheets pipelines: parse with excel_parse, then write via sheets_write_range or sheets_append_rows.
5. Report what you did with file IDs, links, and which account it was done on so the user can verify.
6. If a tool returns an error, read the error message and adapt — do not silently ignore.
"""


Emit = Callable[[dict], Awaitable[None]]


def _strip_mcp_prefix(tool_name: str) -> str:
    """Convert SDK-qualified name to bare tool name. e.g. mcp__gworkagent__drive_list_files → drive_list_files"""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name


class AgentSession:
    """Persistent agent session backed by claude-agent-sdk (uses `claude` CLI auth, no API key)."""

    def __init__(self, policy: Policy):
        self.policy = policy
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._client: ClaudeSDKClient | None = None
        self._current_emit: Emit | None = None
        self._mcp_server = build_sdk_mcp_server()

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

    # Built-in Claude Code CLI tools we never want exposed to this agent.
    # The agent should only operate via the 28 mcp__gworkagent__* tools.
    _BLOCKED_BUILTINS = [
        "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
        "NotebookEdit", "Task", "TodoWrite", "WebFetch", "WebSearch",
        "ToolSearch", "BashOutput", "KillBash", "ExitPlanMode",
    ]

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            all_tools = [f"mcp__{MCP_SERVER_NAME}__{name}" for name in POLICY_OP_BY_TOOL]
            options = ClaudeAgentOptions(
                mcp_servers={MCP_SERVER_NAME: self._mcp_server},
                allowed_tools=all_tools,
                disallowed_tools=self._BLOCKED_BUILTINS,
                can_use_tool=self._can_use_tool,
                system_prompt=SYSTEM_PROMPT,
                permission_mode="default",
                setting_sources=[],
            )
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            self._client = client
        return self._client

    async def run_turn(self, user_message: str, emit: Emit) -> None:
        self._current_emit = emit
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
