"""Tests for `src/tools/_vendor_http.py`.

The module is two thin helpers around urllib.request, but it's now the
shared call-site for `edo.py` and `social.py`. The classifier vocabulary
(`error_kind`) matches `_classify_http_error` so the agent sees
consistent error labels regardless of vendor.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from src.tools import _vendor_http as vh


# ============================================================
# Helpers
# ============================================================

def _make_ok_response(payload: bytes, status: int = 200):
    m = MagicMock()
    m.read.return_value = payload
    m.status = status
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: None
    return m


def _make_http_error(code: int, body: bytes = b'{"err":"x"}'):
    fake = MagicMock()
    fake.read.return_value = body
    return HTTPError("u", code, "msg", {}, fake)


# ============================================================
# classifier
# ============================================================

@pytest.mark.parametrize("code,kind", [
    (400, "bad_input"),
    (401, "permission"),
    (403, "permission"),
    (404, "not_found"),
    (422, "bad_input"),
    (429, "rate_limit"),
    (500, "server"),
    (502, "server"),
    (503, "server"),
    (504, "server"),
])
def test_classify_maps_status_to_kind(code, kind):
    assert vh._classify(code) == kind


# ============================================================
# get_json
# ============================================================

def test_get_json_success_envelope():
    with patch("urllib.request.urlopen",
               return_value=_make_ok_response(b'{"x":1}')):
        out = vh.get_json("https://example.com/api")
    assert out["ok"] is True
    assert out["data"] == {"x": 1}
    assert out["_meta"]["http_status"] == 200


def test_get_json_passes_headers():
    captured = {}

    def fake_urlopen(req, timeout):
        # urllib.request.Request — headers normalized to title case
        captured["headers"] = dict(req.headers)
        return _make_ok_response(b'{}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vh.get_json("https://example.com/api",
                    headers={"Authorization": "Bearer X"})
    # urllib lowercases-then-title-cases header keys
    assert captured["headers"].get("Authorization") == "Bearer X"


@pytest.mark.parametrize("code,kind", [
    (401, "permission"), (403, "permission"), (404, "not_found"),
    (429, "rate_limit"), (500, "server"),
])
def test_get_json_http_error_carries_error_kind(code, kind):
    with patch("urllib.request.urlopen",
               side_effect=_make_http_error(code)):
        out = vh.get_json("https://example.com/api")
    assert out["ok"] is False
    assert out["error_kind"] == kind
    assert out["_meta"]["http_status"] == code


def test_get_json_truncates_long_error_body():
    long_body = b"x" * 5000
    with patch("urllib.request.urlopen",
               side_effect=_make_http_error(500, long_body)):
        out = vh.get_json("https://example.com/api")
    # Truncated to 300 chars to keep agent context small
    assert len(out["error"]) <= 300


# ============================================================
# post_json
# ============================================================

def test_post_json_success_envelope():
    with patch("urllib.request.urlopen",
               return_value=_make_ok_response(b'{"x":1}')):
        out = vh.post_json("https://example.com/api", {"a": 1})
    assert out["ok"] is True
    assert out["data"] == {"x": 1}


def test_post_json_serializes_body():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        return _make_ok_response(b'{}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vh.post_json("https://example.com/api", {"a": 1, "b": "x"})
    body = json.loads(captured["data"])
    assert body == {"a": 1, "b": "x"}


def test_post_json_sets_default_content_type():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        return _make_ok_response(b'{}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vh.post_json("https://example.com/api", {})
    assert captured["headers"].get("Content-type") == "application/json"
    assert captured["headers"].get("Accept") == "application/json"


def test_post_json_caller_can_override_content_type():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        return _make_ok_response(b'{}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vh.post_json("https://example.com/api", {},
                     headers={"Content-Type": "application/x-custom"})
    # Caller wins on duplicate keys
    assert captured["headers"].get("Content-type") == "application/x-custom"


def test_post_json_http_error_envelope():
    with patch("urllib.request.urlopen",
               side_effect=_make_http_error(429, b'{"e":"too many"}')):
        out = vh.post_json("https://example.com/api", {})
    assert out["ok"] is False
    assert out["error_kind"] == "rate_limit"
    assert "too many" in out["error"]


# ============================================================
# downstream callers still work
# ============================================================

def test_edo_module_reuses_shared_helpers():
    from src.tools import edo, _vendor_http
    # edo's `_get_json` / `_post_json` are now aliases of vh.get_json/post_json
    assert edo._get_json is _vendor_http.get_json
    assert edo._post_json is _vendor_http.post_json


def test_social_module_reuses_shared_helpers():
    from src.tools import social, _vendor_http
    assert social._get_json is _vendor_http.get_json
    assert social._post_json is _vendor_http.post_json
