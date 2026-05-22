"""Unit tests for src/tools/_gdoc_url.py."""
from src.tools import _gdoc_url


def test_parse_gdoc_edit_url():
    url = "https://docs.google.com/document/d/1LWElnPo2hyxU62gAT1wrsgNcgiENsEKoiZyoDBR9NQc/edit?usp=sharing"
    r = _gdoc_url.parse(url)
    assert r == {"kind": "gdoc", "document_id": "1LWElnPo2hyxU62gAT1wrsgNcgiENsEKoiZyoDBR9NQc"}


def test_parse_gdoc_minimal_url():
    url = "https://docs.google.com/document/d/AbCdEf_1234567890XYZ/edit"
    r = _gdoc_url.parse(url)
    assert r["kind"] == "gdoc"
    assert r["document_id"] == "AbCdEf_1234567890XYZ"


def test_parse_gsheet_url_with_gid():
    url = "https://docs.google.com/spreadsheets/d/1abcDEF234567890_-XYZ/edit#gid=12345"
    r = _gdoc_url.parse(url)
    assert r == {"kind": "gsheet", "document_id": "1abcDEF234567890_-XYZ"}


def test_parse_gfile_view_url():
    url = "https://drive.google.com/file/d/1abcDEFghi567890_-XYZ/view?usp=sharing"
    r = _gdoc_url.parse(url)
    assert r == {"kind": "gfile", "document_id": "1abcDEFghi567890_-XYZ"}


def test_parse_gfile_open_url():
    url = "https://drive.google.com/open?id=1abcDEFghi567890_-XYZ"
    r = _gdoc_url.parse(url)
    assert r == {"kind": "gfile", "document_id": "1abcDEFghi567890_-XYZ"}


def test_parse_returns_none_for_non_google_url():
    assert _gdoc_url.parse("https://example.com/foo") is None
    assert _gdoc_url.parse("https://google.com/search?q=test") is None


def test_parse_returns_none_for_empty_or_invalid():
    assert _gdoc_url.parse("") is None
    assert _gdoc_url.parse("   ") is None
    assert _gdoc_url.parse(None) is None  # type: ignore[arg-type]
    assert _gdoc_url.parse(123) is None  # type: ignore[arg-type]


def test_is_google_url():
    assert _gdoc_url.is_google_url("https://docs.google.com/document/d/abc1234567890defghij/edit") is True
    assert _gdoc_url.is_google_url("https://example.com/file.pdf") is False
    assert _gdoc_url.is_google_url("") is False


def test_parse_handles_short_id_as_no_match():
    """IDs shorter than ~20 chars are suspicious — pattern requires {20,}."""
    url = "https://docs.google.com/document/d/short123/edit"
    assert _gdoc_url.parse(url) is None
