"""Smoke tests for static/index.html — keep critical icon/component
references in sync. We don't run JS here; these are file-content checks."""
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------- bug_021 lockdown: ToolFocusBadge references icon 'filter' ----------

def test_filter_icon_imported_from_lucide(html: str):
    """The Filter component must be imported from lucide-preact."""
    assert ", Filter," in html or "Filter,\n" in html, (
        "Filter not imported in static/index.html"
    )


def test_filter_icon_registered_in_icons_dict(html: str):
    """REGRESSION: ToolFocusBadge calls `<Icon name='filter' />`. If the
    `filter` key is missing from the ICONS dict, the component falls back
    to `box` — wrong visual for a tool-focus indicator."""
    assert "filter: Filter" in html, "ICONS dict missing 'filter' entry"


def test_tool_focus_badge_still_references_filter_icon(html: str):
    """If someone renames the icon in ToolFocusBadge, this fails loudly."""
    assert 'name="filter"' in html, (
        "ToolFocusBadge no longer references the 'filter' icon — "
        "update test or check the rename was intentional"
    )
