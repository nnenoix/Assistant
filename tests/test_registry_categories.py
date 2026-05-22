"""Tests for tool categorization in registry.py."""
import pytest

from src.tools import registry


def test_every_tool_has_category():
    for t in registry.TOOLS:
        assert t.get("category"), f"tool {t['name']} missing category"
        assert isinstance(t["category"], str)


def test_category_derived_from_name_prefix():
    """When no explicit category, prefix derivation works."""
    by_name = {t["name"]: t for t in registry.TOOLS}
    assert by_name["sheets_read_range"]["category"] == "sheets"
    assert by_name["drive_search"]["category"] == "drive"
    assert by_name["gmail_search"]["category"] == "gmail"


def test_list_categories_groups_correctly():
    cats = registry.list_categories()
    # sheets should have many tools
    assert "sheets" in cats
    assert len(cats["sheets"]) > 10
    # Every tool in cats["sheets"] starts with sheets_
    for name in cats["sheets"]:
        assert name.startswith("sheets_")


def test_select_tools_by_single_category():
    sel = registry.select_tools("sheets")
    assert sel  # non-empty
    for t in sel:
        assert t["category"] == "sheets"


def test_select_tools_by_list():
    sel = registry.select_tools(["gmail", "calendar"])
    cats = {t["category"] for t in sel}
    assert cats == {"gmail", "calendar"}


def test_select_tools_empty_returns_empty():
    assert registry.select_tools([]) == []
    assert registry.select_tools(set()) == []


def test_select_tools_unknown_category_returns_empty():
    assert registry.select_tools(["totally_made_up"]) == []


def test_category_counts_sum_to_total():
    cats = registry.list_categories()
    total = sum(len(names) for names in cats.values())
    assert total == len(registry.TOOLS)


def test_no_tools_have_misc_category():
    """All tools should have a real category, not the misc fallback.
    If misc appears, someone added a tool with no underscore in the name."""
    misc_tools = [t["name"] for t in registry.TOOLS if t["category"] == "misc"]
    assert not misc_tools, f"these tools fell back to misc: {misc_tools}"
