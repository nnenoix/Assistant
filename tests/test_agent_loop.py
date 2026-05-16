import asyncio
from unittest.mock import MagicMock

import pytest

from src.agent import AgentSession


def make_anthropic_response(content_blocks, stop_reason="end_turn"):
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def text_block(text):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def tool_block(id, name, input):
    b = MagicMock()
    b.type = "tool_use"
    b.id = id
    b.name = name
    b.input = input
    return b


def _tool_entry(name, fn, policy_op):
    """Helper: build a tool dict matching what registry.BY_NAME produces."""
    return {
        "fn": fn,
        "policy_op": policy_op,
        "schema": {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}},
    }


@pytest.mark.asyncio
async def test_simple_text_response_no_tools():
    fake_client = MagicMock()
    fake_client.messages.create.return_value = make_anthropic_response([text_block("Hello!")])
    session = AgentSession(client=fake_client, policy=MagicMock(), tools={})

    events = []
    async def emit(e): events.append(e)

    await session.run_turn("hi", emit)

    text_events = [e for e in events if e["type"] == "text"]
    assert any("Hello!" in e["text"] for e in text_events)
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_allowed_tool_runs_silently():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_1", "drive_list_files", {"folder_id": "root"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Done.")]),
    ]
    fake_policy = MagicMock()
    fake_policy.is_allowed.return_value = True
    tools = {"drive_list_files": _tool_entry("drive_list_files", lambda **kw: [{"id": "1"}], "drive.read")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)

    events = []
    async def emit(e): events.append(e)

    await session.run_turn("list", emit)

    fake_policy.is_allowed.assert_called_with("drive.read", {"folder_id": "root"})
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "drive_list_files"
    assert not any(e["type"] == "approval_required" for e in events)


@pytest.mark.asyncio
async def test_denied_tool_waits_for_approval_and_runs_on_approve():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_2", "drive_delete", {"file_id": "X"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Deleted.")]),
    ]
    fake_policy = MagicMock(); fake_policy.is_allowed.return_value = False
    called = []
    tools = {"drive_delete": _tool_entry("drive_delete", lambda **kw: called.append(kw) or None, "drive.delete")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)

    events = []
    async def emit(e): events.append(e)

    task = asyncio.create_task(session.run_turn("delete X", emit))
    await asyncio.sleep(0.05)

    pending = [e for e in events if e["type"] == "approval_required"]
    assert len(pending) == 1
    request_id = pending[0]["request_id"]

    session.resolve_approval(request_id, approved=True)
    await task

    assert called == [{"file_id": "X"}]


@pytest.mark.asyncio
async def test_denied_tool_returns_error_on_deny():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        make_anthropic_response([tool_block("tu_3", "drive_delete", {"file_id": "X"})], stop_reason="tool_use"),
        make_anthropic_response([text_block("Skipped.")]),
    ]
    fake_policy = MagicMock(); fake_policy.is_allowed.return_value = False
    deleted = []
    tools = {"drive_delete": _tool_entry("drive_delete", lambda **kw: deleted.append(kw), "drive.delete")}

    session = AgentSession(client=fake_client, policy=fake_policy, tools=tools)
    events = []
    async def emit(e): events.append(e)

    task = asyncio.create_task(session.run_turn("delete X", emit))
    await asyncio.sleep(0.05)

    request_id = next(e["request_id"] for e in events if e["type"] == "approval_required")
    session.resolve_approval(request_id, approved=False)
    await task

    assert deleted == []  # function not called
    # Second call should have received a tool_result with is_error
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    last_msg = second_call_kwargs["messages"][-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"][0]["is_error"] is True
