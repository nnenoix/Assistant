"""Tests for the agent's policy/approval glue.

The agent uses claude-agent-sdk under the hood; the SDK iteration loop itself is
trusted to work as documented. These tests focus on what we own: the
`_can_use_tool` permission callback, the approval-future flow, and the
MCP-prefix stripping.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.agent import AgentSession, _strip_mcp_prefix
from src.tools.registry import MCP_SERVER_NAME


def test_strip_mcp_prefix_known():
    assert _strip_mcp_prefix(f"mcp__{MCP_SERVER_NAME}__drive_list_files") == "drive_list_files"


def test_strip_mcp_prefix_unknown_passthrough():
    assert _strip_mcp_prefix("Bash") == "Bash"
    assert _strip_mcp_prefix("mcp__other__foo") == "mcp__other__foo"


@pytest.mark.asyncio
async def test_can_use_tool_allowed_returns_allow():
    policy = MagicMock()
    policy.is_allowed.return_value = True
    session = AgentSession(policy=policy)

    result = await session._can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__drive_list_files",
        {"folder_id": "root"},
        context=None,
    )
    assert isinstance(result, PermissionResultAllow)
    policy.is_allowed.assert_called_with("drive.read", {"folder_id": "root"})


@pytest.mark.asyncio
async def test_can_use_tool_unknown_tool_denied():
    session = AgentSession(policy=MagicMock())
    result = await session._can_use_tool("Bash", {"command": "rm -rf /"}, context=None)
    assert isinstance(result, PermissionResultDeny)


@pytest.mark.asyncio
async def test_can_use_tool_denied_then_approved():
    policy = MagicMock()
    policy.is_allowed.return_value = False
    session = AgentSession(policy=policy)

    events = []
    async def emit(e): events.append(e)
    session._current_emit = emit

    task = asyncio.create_task(session._can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__drive_delete",
        {"file_id": "X"},
        context=None,
    ))
    await asyncio.sleep(0.05)

    pending = [e for e in events if e["type"] == "approval_required"]
    assert len(pending) == 1
    request_id = pending[0]["request_id"]
    assert pending[0]["name"] == "drive_delete"
    assert pending[0]["policy_op"] == "drive.delete"

    session.resolve_approval(request_id, approved=True)
    result = await task

    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_can_use_tool_denied_then_rejected():
    policy = MagicMock()
    policy.is_allowed.return_value = False
    session = AgentSession(policy=policy)

    events = []
    async def emit(e): events.append(e)
    session._current_emit = emit

    task = asyncio.create_task(session._can_use_tool(
        f"mcp__{MCP_SERVER_NAME}__drive_delete",
        {"file_id": "X"},
        context=None,
    ))
    await asyncio.sleep(0.05)

    request_id = next(e["request_id"] for e in events if e["type"] == "approval_required")
    session.resolve_approval(request_id, approved=False)
    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert any(e["type"] == "tool_denied" for e in events)


def test_resolve_approval_unknown_id_is_noop():
    session = AgentSession(policy=MagicMock())
    # should not raise
    session.resolve_approval("does-not-exist", approved=True)
