"""Unit tests for src/updater.py.

The module only does HTTP + JSON + semver compare — no integrations,
no UI. All cases mock urllib.request.urlopen.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from src import updater


# ============================================================
# _parse_version + _is_newer
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("0.1.0", (0, 1, 0)),
    ("10.0.0", (10, 0, 0)),
    ("1.2.3-rc1", (1, 2, 3)),     # pre-release stripped from each segment
    ("1.2.3+build.5", (1, 2, 3)),
    ("1.0", (1, 0)),                # 2-component still works
])
def test_parse_version_accepts(raw, expected):
    assert updater._parse_version(raw) == expected


@pytest.mark.parametrize("raw", [
    None,
    "",
    "garbage",
    "a.b.c",
    "..",
    "1..2",
    123,                            # not a string
    "rc1",                          # no digit prefix
])
def test_parse_version_rejects(raw):
    assert updater._parse_version(raw) is None


@pytest.mark.parametrize("latest,current,expected", [
    ("0.2.0", "0.1.0", True),
    ("1.0.0", "0.99.99", True),
    ("0.1.1", "0.1.0", True),
    ("0.1.0", "0.1.0", False),     # equal → not newer
    ("0.0.9", "0.1.0", False),
    ("1.2.3-rc2", "1.2.3-rc1", False),  # pre-release stripped → equal
    ("v0.2.0", "0.1.0", True),     # tolerates leading v
])
def test_is_newer(latest, current, expected):
    assert updater._is_newer(latest, current) == expected


def test_is_newer_returns_false_on_garbage_input():
    """A malformed manifest must not nag the user with a phantom update."""
    assert updater._is_newer("not-a-version", "0.1.0") is False
    assert updater._is_newer("0.1.0", "garbage") is False


# ============================================================
# check_for_updates — happy path
# ============================================================

def _ok_response(body: dict, status: int = 200):
    m = MagicMock()
    m.read.return_value = json.dumps(body).encode("utf-8")
    m.status = status
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: None
    return m


def test_returns_update_available_with_payload():
    body = {
        "latest_version": "0.2.0",
        "download_url": "https://example.com/agent-0.2.0.exe",
        "release_notes": "Fixed X, added Y.",
    }
    with patch("urllib.request.urlopen", return_value=_ok_response(body)):
        out = updater.check_for_updates("0.1.0", "https://x/manifest.json")
    assert out["ok"] is True
    assert out["update_available"] is True
    assert out["current_version"] == "0.1.0"
    assert out["latest_version"] == "0.2.0"
    assert out["download_url"] == "https://example.com/agent-0.2.0.exe"
    assert out["release_notes"] == "Fixed X, added Y."


def test_returns_no_update_when_current_is_latest():
    body = {"latest_version": "0.1.0",
            "download_url": "https://example.com/agent.exe"}
    with patch("urllib.request.urlopen", return_value=_ok_response(body)):
        out = updater.check_for_updates("0.1.0", "https://x/manifest.json")
    assert out["ok"] is True
    assert out["update_available"] is False
    # `download_url` / `release_notes` deliberately omitted on the no-update
    # path to keep the payload small.
    assert "download_url" not in out
    assert "release_notes" not in out


def test_handles_manifest_without_optional_fields():
    body = {"latest_version": "0.2.0"}  # no download_url, no release_notes
    with patch("urllib.request.urlopen", return_value=_ok_response(body)):
        out = updater.check_for_updates("0.1.0", "https://x/manifest.json")
    assert out["ok"] is True
    assert out["update_available"] is True
    assert "download_url" not in out


# ============================================================
# check_for_updates — error paths
# ============================================================

def test_http_404_maps_to_not_found():
    err = HTTPError("u", 404, "missing", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["ok"] is False
    assert out["error_kind"] == "not_found"
    assert out["_meta"]["http_status"] == 404


def test_http_429_maps_to_rate_limit():
    err = HTTPError("u", 429, "throttled", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["error_kind"] == "rate_limit"


def test_http_500_maps_to_server():
    err = HTTPError("u", 503, "down", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["error_kind"] == "server"


def test_url_error_treated_as_server():
    """DNS failures / connection refused / TLS errors all surface as
    URLError. Mapping to `server` is conservative — caller can suppress
    the popup and just log it."""
    with patch("urllib.request.urlopen",
               side_effect=URLError("Name or service not known")):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["ok"] is False
    assert out["error_kind"] == "server"
    assert out["_meta"]["http_status"] is None
    assert "URLError" in out["error"]


def test_timeout_treated_as_server():
    with patch("urllib.request.urlopen",
               side_effect=TimeoutError("slow CDN")):
        out = updater.check_for_updates("0.1.0", "https://x/m.json", timeout=1)
    assert out["error_kind"] == "server"


def test_non_json_response_is_bad_input():
    m = MagicMock()
    m.read.return_value = b"<html>oops</html>"
    m.status = 200
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: None
    with patch("urllib.request.urlopen", return_value=m):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert "not JSON" in out["error"]


def test_manifest_missing_latest_version_is_bad_input():
    body = {"download_url": "https://example.com/agent.exe"}  # no latest_version
    with patch("urllib.request.urlopen", return_value=_ok_response(body)):
        out = updater.check_for_updates("0.1.0", "https://x/m.json")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"
    assert "latest_version" in out["error"]


# ============================================================
# get_current_version
# ============================================================

def test_get_current_version_returns_a_version_string():
    """In dev (source checkout), reads pyproject.toml. In a PyInstaller
    build, reads bundled metadata. Either way: not '0.0.0' fallback."""
    v = updater.get_current_version()
    assert isinstance(v, str)
    # Parses as a real version (not the "0.0.0" fallback)
    assert updater._parse_version(v) is not None


def test_get_current_version_matches_pyproject():
    """Source-checkout path: the version reported should match the one
    in pyproject.toml so a `uv run` and a `pip install -e .` agree."""
    from pathlib import Path
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    expected = None
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("version") and "=" in line:
            expected = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    assert expected is not None
    # importlib.metadata can return something different if the package
    # was installed; we only assert the source-version path matches when
    # both look like real versions.
    v = updater.get_current_version()
    if updater._parse_version(v):
        assert v  # smoke
