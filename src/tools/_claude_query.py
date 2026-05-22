"""Claude sub-query helper (Phase 15) — uses CLI subscription auth, NOT API key.

Per project constraint (user explicit): ANTHROPIC_API_KEY will never be set up
for this project. All LLM access goes through claude CLI's OAuth/keychain
auth (claude-agent-sdk underneath). This module wraps `claude_agent_sdk.query()`
as a "give me text response for this prompt with this model" primitive.

Concurrency: `query()` spawns a subprocess per call (the `claude` CLI itself).
Multiple `query()` calls in parallel via asyncio.gather → multiple subprocesses
side-by-side. Subprocess overhead is ~1-3s cold start; the LLM time dominates
for non-trivial prompts.

Isolation from the parent agent:
- `mcp_servers={}` — don't inherit the parent's tool registry (this is for
  pure analysis, no tool use needed)
- `tools=[]` and `allowed_tools=[]` — disable all built-in tools too
- `setting_sources=[]` — skip user/project/local CLAUDE.md (clean context)
- `permission_mode="bypassPermissions"` — non-interactive
- `max_turns=1` — single turn, no agentic loop
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)


# Whitelist of accepted model identifiers. Prevents any caller from sneaking
# arbitrary strings into ClaudeAgentOptions(model=...) that might surface in
# subprocess args. Aliases (haiku/sonnet/opus) resolve to defaults inside CLI.
ALLOWED_MODELS: frozenset[str] = frozenset({
    "haiku",
    "sonnet",
    "opus",
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
})

# Hard upper bound on a single sub-LLM call. Apps Script's max is 6 min;
# a Claude call should complete much faster (~30-60s typical, ~120s extreme).
# 300s gives generous headroom while still preventing a forgotten subprocess
# from hanging the parent forever.
_CALL_TIMEOUT_S = 300


async def call(
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    """Call Claude with the given (system, user) prompts. Returns text response.

    Uses claude CLI subscription auth — NO ANTHROPIC_API_KEY needed.

    Args:
      model: model alias ("haiku", "sonnet", "opus") or full name like "claude-haiku-4-5".
             Must be in ALLOWED_MODELS — arbitrary strings raise ValueError.
      system_prompt: the system prompt for this single-turn call
      user_message: the user message (any length — passed via SDK, not CLI args)

    Returns:
      Concatenated text of all assistant TextBlocks.

    Raises:
      ValueError if model is not in ALLOWED_MODELS.
      asyncio.TimeoutError if call exceeds _CALL_TIMEOUT_S (300s).
      RuntimeError if the model produces no text response.
    """
    if model not in ALLOWED_MODELS:
        raise ValueError(
            f"Model {model!r} not in allowlist. Use one of: "
            f"{sorted(ALLOWED_MODELS)}"
        )

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        permission_mode="bypassPermissions",
        max_turns=1,
        setting_sources=[],
        skills=None,
    )

    async def _collect() -> list[str]:
        parts: list[str] = []
        async for msg in query(prompt=user_message, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        return parts

    parts = await asyncio.wait_for(_collect(), timeout=_CALL_TIMEOUT_S)

    if not parts:
        raise RuntimeError(
            f"Claude returned no text response (model={model}). "
            "Check that the CLI is logged in: `claude --version` and your "
            "subscription is active."
        )
    return "\n".join(parts)
