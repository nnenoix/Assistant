"""Unit tests for _meta_warning_prefix in registry.py."""
from src.tools.registry import _meta_warning_prefix


def test_no_prefix_when_clean():
    """A normal successful result without trouble flags → no prefix."""
    result = {
        "values": [["a", "b"], ["c", "d"]],
        "_meta": {"range_read": "S!A1:B2", "row_count": 2},
    }
    assert _meta_warning_prefix(result) is None


def test_no_prefix_when_no_meta():
    assert _meta_warning_prefix({"foo": "bar"}) is None


def test_no_prefix_for_non_dict():
    assert _meta_warning_prefix("string") is None
    assert _meta_warning_prefix([1, 2, 3]) is None
    assert _meta_warning_prefix(42) is None


def test_no_prefix_for_no_data_empty_reason():
    """empty_reason='no_data' is benign — valid but empty range."""
    result = {"values": [], "_meta": {"empty_reason": "no_data", "row_count": 0}}
    assert _meta_warning_prefix(result) is None


def test_prefix_on_truncated():
    result = {
        "files": [{"id": "1"}],
        "_meta": {"truncated": True, "truncation_reason": "more results available — page_size=50"},
    }
    prefix = _meta_warning_prefix(result)
    assert prefix is not None
    assert "⚠️ META" in prefix
    assert "truncated" in prefix
    assert prefix.endswith("\n\n")


def test_prefix_on_invalid_range_empty_reason():
    result = {"matches": [], "_meta": {"empty_reason": "invalid_range"}}
    prefix = _meta_warning_prefix(result)
    assert "empty_reason=invalid_range" in prefix


def test_prefix_on_no_matches_empty_reason():
    """no_matches is also worth flagging — distinguishes from no_data."""
    result = {"matches": [], "_meta": {"empty_reason": "no_matches"}}
    prefix = _meta_warning_prefix(result)
    assert "empty_reason=no_matches" in prefix


def test_prefix_on_calendar_default_window():
    """Calendar's nested window.default_used should fire prefix."""
    result = {
        "events": [],
        "_meta": {
            "window": {"time_min": "2026-05-21T00:00:00Z", "default_used": True},
            "returned_count": 0,
        },
    }
    prefix = _meta_warning_prefix(result)
    assert "default_window_used" in prefix


def test_prefix_on_flat_default_used():
    """Some tools may set default_used at top level — also handled."""
    result = {"items": [], "_meta": {"default_used": True}}
    prefix = _meta_warning_prefix(result)
    assert "default_window_used" in prefix


def test_default_window_dedup_when_both_flat_and_nested():
    result = {
        "events": [],
        "_meta": {
            "default_used": True,
            "window": {"default_used": True},
        },
    }
    prefix = _meta_warning_prefix(result)
    # Should appear exactly once, not twice
    assert prefix.count("default_window_used") == 1


def test_prefix_on_semantic_fallback_to_substring():
    result = {
        "results": [],
        "_meta": {"search_method": "substring", "fallback_reason": "embeddings unavailable"},
    }
    prefix = _meta_warning_prefix(result)
    assert "semantic_fell_back_to_substring" in prefix


def test_no_prefix_on_clean_semantic_search():
    result = {"results": [{"score": 0.9}], "_meta": {"search_method": "semantic"}}
    assert _meta_warning_prefix(result) is None


def test_multiple_flags_combined():
    result = {
        "matches": [],
        "_meta": {
            "truncated": True,
            "truncation_reason": "page cap",
            "empty_reason": "no_matches",
        },
    }
    prefix = _meta_warning_prefix(result)
    assert "truncated" in prefix
    assert "empty_reason=no_matches" in prefix
    # Single line, joined with '; '
    assert "; " in prefix


def test_truncation_reason_capped_at_80_chars():
    """Long truncation_reason gets shortened to keep the prefix compact."""
    long_reason = "x" * 200
    result = {"_meta": {"truncated": True, "truncation_reason": long_reason}}
    prefix = _meta_warning_prefix(result)
    # The single-flag block should not contain the full 200 chars
    assert "x" * 200 not in prefix
