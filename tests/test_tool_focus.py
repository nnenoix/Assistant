"""Tests for AgentSession.set_tool_focus / _allowed_tool_names (Phase 13F).

These tests bypass the actual SDK by mocking ClaudeSDKClient construction.
What we verify is the FILTER LOGIC — that the right list of mcp tool names
flows into ClaudeAgentOptions.allowed_tools given a focus set.
"""
import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.agent import AgentSession
from src.policy import Policy
from src.tools.registry import MCP_SERVER_NAME, TOOLS


def _make_session() -> AgentSession:
    """Build an AgentSession with an empty/permissive policy for testing."""
    policy = Policy({"allow": {}})  # empty allowlist — all tools require approval
    return AgentSession(policy=policy)


def test_default_focus_is_none_and_allows_all_tools():
    s = _make_session()
    assert s._tool_focus is None
    allowed = s._allowed_tool_names()
    # When focus is None, every registered tool is allowed
    assert len(allowed) == len(TOOLS)
    # All names are MCP-prefixed
    assert all(name.startswith(f"mcp__{MCP_SERVER_NAME}__") for name in allowed)


def test_set_tool_focus_narrows_to_categories():
    s = _make_session()
    # Use a synchronous wrapper around the async method for this assertion
    import asyncio
    asyncio.run(s.set_tool_focus(["sheets", "drive"]))
    allowed = s._allowed_tool_names()
    # Should have only sheets_ and drive_ tools
    short_names = [n.replace(f"mcp__{MCP_SERVER_NAME}__", "") for n in allowed]
    assert all(n.startswith(("sheets_", "drive_")) for n in short_names), \
        f"got non-sheets/drive: {[n for n in short_names if not n.startswith(('sheets_', 'drive_'))]}"
    # And should include AT LEAST one each
    assert any(n.startswith("sheets_") for n in short_names)
    assert any(n.startswith("drive_") for n in short_names)


def test_set_tool_focus_none_clears_filter():
    s = _make_session()
    import asyncio
    asyncio.run(s.set_tool_focus(["sheets"]))
    sheets_only = len(s._allowed_tool_names())
    asyncio.run(s.set_tool_focus(None))
    assert len(s._allowed_tool_names()) == len(TOOLS)
    assert sheets_only < len(TOOLS)


def test_set_tool_focus_unknown_category_yields_empty_list():
    s = _make_session()
    import asyncio
    asyncio.run(s.set_tool_focus(["totally_made_up_category"]))
    assert s._allowed_tool_names() == []


def test_set_tool_focus_same_value_is_noop_on_session_state():
    """Setting the SAME focus twice shouldn't toggle session close — important
    because close() is expensive and breaks conversation continuity."""
    s = _make_session()
    import asyncio
    asyncio.run(s.set_tool_focus(["sheets", "drive"]))
    fs1 = s._tool_focus
    asyncio.run(s.set_tool_focus(["drive", "sheets"]))  # same set, different order
    assert fs1 == s._tool_focus


def test_dynamic_tool_routing_env_flag_default_off():
    """Without env var, _tool_focus_auto should be False."""
    if "DYNAMIC_TOOL_ROUTING" in os.environ:
        with patch.dict(os.environ, {}, clear=True):
            s = _make_session()
            assert s._tool_focus_auto is False
    else:
        s = _make_session()
        assert s._tool_focus_auto is False


def test_dynamic_tool_routing_env_flag_enables_auto():
    with patch.dict(os.environ, {"DYNAMIC_TOOL_ROUTING": "1"}):
        s = _make_session()
        assert s._tool_focus_auto is True


def test_set_tool_focus_reduces_context_size_significantly():
    """The point of the feature: a focused session should expose far fewer
    mcp tool names than the full set."""
    s = _make_session()
    full_count = len(s._allowed_tool_names())
    import asyncio
    asyncio.run(s.set_tool_focus(["calendar"]))
    focused_count = len(s._allowed_tool_names())
    # At least 80% reduction expected for a single narrow category
    assert focused_count < full_count * 0.2, (
        f"focused ({focused_count}) should be <20% of full ({full_count})"
    )
