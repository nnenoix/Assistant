"""Tool-response truncation envelope tests.

When a tool returns more than `MAX_TOOL_PAYLOAD` chars, the wrapper wraps
the output in a structured envelope so the agent can still json.parse it.
Previously the output was naively cut mid-string, leaving invalid JSON
the agent had to text-scan.
"""
import asyncio
import json

import pytest

from src.tools import registry


def test_truncate_payload_stays_within_budget():
    """Envelope total length must not exceed MAX_TOOL_PAYLOAD by more than
    a few bytes (the warn-prefix is empty in this case)."""
    huge = json.dumps({"data": "x" * 60_000})
    out = registry._truncate_payload(huge, "sheets_read_range", None)
    assert len(out) <= registry.MAX_TOOL_PAYLOAD


def test_truncate_payload_is_valid_json():
    """The whole point: agent can json.parse the truncated response."""
    huge = json.dumps({"data": "x" * 60_000})
    out = registry._truncate_payload(huge, "drive_list_files", None)
    parsed = json.loads(out)
    assert parsed["_truncated"] is True
    assert isinstance(parsed["preview"], str)
    assert parsed["_meta"]["truncated_by_payload"] is True


def test_truncate_payload_envelope_carries_full_size():
    huge = json.dumps({"data": "y" * 80_000})
    out = registry._truncate_payload(huge, "sheets_read_range", None)
    parsed = json.loads(out)
    assert parsed["_meta"]["full_payload_chars"] == len(huge)
    assert parsed["_meta"]["shown_chars"] < parsed["_meta"]["full_payload_chars"]


def test_truncate_payload_includes_tool_specific_hint():
    """Hint must match the per-tool guidance from `_truncation_hint`."""
    huge = json.dumps({"data": "z" * 60_000})
    out_sheets = registry._truncate_payload(huge, "sheets_read_range", None)
    out_drive = registry._truncate_payload(huge, "drive_list_files", None)
    h_sheets = json.loads(out_sheets)["_meta"]["hint"]
    h_drive = json.loads(out_drive)["_meta"]["hint"]
    assert "sheets_summarize" in h_sheets
    assert "page_size" in h_drive or "drive_search" in h_drive


def test_truncate_payload_preserves_meta_warn_prefix():
    """If the original output had a `⚠️ META: ...` warning prefix, it should
    survive truncation (otherwise the agent loses signal about the data)."""
    huge = json.dumps({"data": "w" * 60_000})
    warn = "⚠️ META: truncated_data | "
    out = registry._truncate_payload(huge, "sheets_read_range", warn)
    assert out.startswith(warn)
    # Strip prefix → still valid JSON
    body = out[len(warn):]
    parsed = json.loads(body)
    assert parsed["_truncated"] is True


def test_wrap_for_sdk_uses_envelope_when_over_budget():
    """End-to-end: a tool returning a 50KB dict should come back as a
    parseable truncation envelope, not a cut string."""
    big_dict = {"items": [{"id": i, "v": "x" * 200} for i in range(300)]}

    def fn():
        return big_dict

    spec = registry._tool(
        "test_big",
        fn,
        "sheets.read",
        "test",
        {"type": "object", "properties": {}},
    )
    wrapped = registry._wrap_for_sdk(spec)
    handler = getattr(wrapped, "handler", wrapped)
    result = asyncio.run(handler({}))
    text = result["content"][0]["text"]
    # Strip any META prefix
    if text.startswith("⚠️"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[text.index("{"):]
    parsed = json.loads(text)
    assert parsed["_truncated"] is True
    assert parsed["_meta"]["full_payload_chars"] > registry.MAX_TOOL_PAYLOAD


def test_max_tool_payload_under_anthropic_25k_budget():
    """Sanity: our char budget must stay under 25k tokens worst-case.
    1 token ≈ 2 chars (Russian/Cyrillic), so 20k chars ≈ 10k tokens — safe."""
    assert registry.MAX_TOOL_PAYLOAD <= 25_000
