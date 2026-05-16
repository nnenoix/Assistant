import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from anthropic import Anthropic

from src.config import DEFAULT_MODEL


SYSTEM_PROMPT = """You are a personal assistant operating on the user's Google Workspace and local machine.

You have tools for:
- Google Drive: list/search/create/upload/download/rename/move/delete/copy files
- Google Sheets: read/write/append ranges, create spreadsheets, add tabs
- Apps Script: clone/pull/push/run script projects via clasp
- Local filesystem: read/write files, list directories
- Excel (.xlsx): parse local workbooks into row dicts

Rules:
1. Always confirm with the user before destructive actions (delete, overwrite) unless they explicitly said "yes, do it" in this turn.
2. When the user references a file/folder by name, search first to find the id, then ask which one if ambiguous.
3. Prefer `sheets_append_rows` over `sheets_write_range` when adding data.
4. For Excel-to-Sheets pipelines: parse with `excel_parse`, then write via `sheets_write_range` or `sheets_append_rows`.
5. Report what you did with file IDs and links so the user can verify.
6. If a tool returns an error, read the error message and adapt — do not silently ignore.
"""


Emit = Callable[[dict], Awaitable[None]]


class AgentSession:
    def __init__(self, client: Anthropic, policy, tools: dict[str, dict], model: str = DEFAULT_MODEL):
        self.client = client
        self.policy = policy
        self.tools = tools
        self.model = model
        self.history: list[dict] = []
        self._pending_approvals: dict[str, asyncio.Future] = {}

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(approved)

    async def run_turn(self, user_message: str, emit: Emit) -> None:
        self.history.append({"role": "user", "content": user_message})

        while True:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=self.model,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=[t["schema"] for t in self.tools.values()] if self.tools else [],
                messages=list(self.history),
            )

            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    await emit({"type": "text", "text": block.text})
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use", "id": block.id, "name": block.name, "input": block.input,
                    })

            self.history.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool = self.tools.get(block.name)
                if tool is None:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {block.name}",
                        "is_error": True,
                    })
                    continue

                allowed = self.policy.is_allowed(tool["policy_op"], block.input)
                if not allowed:
                    request_id = str(uuid.uuid4())
                    fut: asyncio.Future = asyncio.get_running_loop().create_future()
                    self._pending_approvals[request_id] = fut
                    await emit({
                        "type": "approval_required",
                        "request_id": request_id,
                        "tool_use_id": block.id,
                        "name": block.name,
                        "input": block.input,
                        "policy_op": tool["policy_op"],
                    })
                    approved = await fut
                    if not approved:
                        await emit({"type": "tool_denied", "tool_use_id": block.id, "name": block.name})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "User denied this action.",
                            "is_error": True,
                        })
                        continue

                await emit({"type": "tool_call", "tool_use_id": block.id, "name": block.name, "input": block.input})
                try:
                    result = await asyncio.to_thread(tool["fn"], **block.input)
                    content_str = json.dumps(result, default=str) if result is not None else "(no output)"
                    await emit({"type": "tool_result", "tool_use_id": block.id, "result_preview": content_str[:500]})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content_str})
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    await emit({"type": "tool_error", "tool_use_id": block.id, "error": err})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": err, "is_error": True})

            self.history.append({"role": "user", "content": tool_results})

        await emit({"type": "done"})
