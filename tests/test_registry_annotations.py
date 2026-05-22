"""MCP tool-annotation coverage tests.

The audit (compass_artifact ws-2f0a..., key finding #1) flagged
`MCP annotations покрытие — 0/236`. Clients of the MCP spec 2025-03-26
default to `destructiveHint=true, openWorldHint=true` for any tool
without annotations, which hurts UX (extra confirmations) and blocks
parallelization of safe read calls. These tests lock down the new
`_annotations_for` derivation + 100% coverage of the registry.
"""
from claude_agent_sdk import ToolAnnotations

from src.tools import registry


def _by_name() -> dict[str, dict]:
    return {t["name"]: t for t in registry.TOOLS}


# ---------- coverage ----------

def test_every_tool_has_annotations():
    """No tool may ship without MCP annotations."""
    missing = [t["name"] for t in registry.TOOLS if t.get("annotations") is None]
    assert not missing, f"{len(missing)} tools missing annotations: {missing[:5]}"


def test_annotations_are_pydantic_models():
    """The `@tool` decorator wants a Pydantic ToolAnnotations, not a dict."""
    for t in registry.TOOLS:
        assert isinstance(t["annotations"], ToolAnnotations), t["name"]


# ---------- verb → hint mapping ----------

def test_read_tool_is_read_only_idempotent():
    """sheets_read_range is `sheets.read` — should be read-only + idempotent."""
    a = _by_name()["sheets_read_range"]["annotations"]
    assert a.readOnlyHint is True
    assert a.destructiveHint is False
    assert a.idempotentHint is True
    assert a.openWorldHint is True  # Google API = external


def test_write_tool_is_destructive_idempotent():
    """sheets_write_range is `sheets.write` — overwrites existing cells, so
    destructive by MCP spec («not additive only»). Idempotent because the
    same args yield the same final state on replay."""
    a = _by_name()["sheets_write_range"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is True
    assert a.idempotentHint is True


def test_delete_tool_destructive_but_idempotent():
    """drive_delete is `drive.delete` — destructive, but a second delete
    on an already-gone file is a no-op so idempotent=true."""
    a = _by_name()["drive_delete"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is True
    assert a.idempotentHint is True


def test_send_tool_destructive_not_idempotent():
    """gmail_send_draft is `gmail.send` — sending the same draft twice
    delivers two emails, so NOT idempotent."""
    a = _by_name()["gmail_send_draft"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is True
    assert a.idempotentHint is False


def test_apps_script_run_destructive_not_idempotent():
    """apps_script.run executes arbitrary code → destructive, not idempotent."""
    a = _by_name()["apps_script_api_run_ad_hoc"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is True
    assert a.idempotentHint is False


def test_create_tool_not_idempotent():
    """drive_create_folder creates a new folder each call → not idempotent."""
    a = _by_name()["drive_create_folder"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is False
    assert a.idempotentHint is False


def test_append_tool_not_idempotent():
    """sheets_append_rows always appends → second call = duplicate rows."""
    a = _by_name()["sheets_append_rows"]["annotations"]
    assert a.idempotentHint is False


def test_draft_tool_not_idempotent():
    """gmail_create_draft creates a new draft each call → not idempotent."""
    a = _by_name()["gmail_create_draft"]["annotations"]
    assert a.readOnlyHint is False
    assert a.destructiveHint is False
    assert a.idempotentHint is False


# ---------- openWorld domain partition ----------

def test_external_api_tools_are_open_world():
    """Tools that touch Google/WB/web are open-world."""
    by_name = _by_name()
    for name in ("drive_search", "gmail_search", "sheets_read_range", "calendar_list_events"):
        assert by_name[name]["annotations"].openWorldHint is True, name


def test_local_tools_are_closed_world():
    """Local fs / self / aliases / notes / chats are NOT open-world."""
    by_name = _by_name()
    for name in ("local_read_file", "aliases_list", "notes_add", "self_list_tools"):
        if name not in by_name:
            continue  # skip if a tool was renamed
        assert by_name[name]["annotations"].openWorldHint is False, name


# ---------- helper unit tests ----------

def test_annotations_for_unknown_verb_returns_none():
    """An unrecognised verb falls back to MCP defaults (no annotation)."""
    assert registry._annotations_for("foo.barbaz") is None


def test_annotations_for_missing_policy_op_returns_none():
    assert registry._annotations_for(None) is None
    assert registry._annotations_for("") is None
    assert registry._annotations_for("noverb") is None


def test_annotations_override_takes_precedence():
    """Explicit annotations on `_tool()` win over policy_op derivation."""
    override = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
    )
    spec = registry._tool(
        "test_synthetic",
        lambda: None,
        "sheets.read",  # would normally produce read-only
        "synthetic test",
        {"type": "object", "properties": {}},
        annotations=override,
    )
    assert spec["annotations"] is override
