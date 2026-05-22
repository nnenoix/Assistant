"""Tests for _classify_exception / _classify_http_error in registry.py."""
from unittest.mock import MagicMock

import pytest

from src.tools.registry import (
    _classify_exception,
    _classify_http_error,
)


# ---- _classify_http_error ----

def test_http_401_is_auth_scope():
    assert _classify_http_error(401, "Unauthorized") == "auth_scope"


def test_http_403_insufficient_scope_is_auth_scope():
    assert _classify_http_error(403, "Request had insufficient authentication scopes") == "auth_scope"
    assert _classify_http_error(403, "insufficient_scope") == "auth_scope"


def test_http_403_generic_is_permission():
    assert _classify_http_error(403, "The caller does not have permission") == "permission"


def test_http_404_is_not_found():
    assert _classify_http_error(404, "Requested entity was not found.") == "not_found"


def test_http_400_is_bad_input():
    assert _classify_http_error(400, "Unable to parse range: Sheet1!ZZ") == "bad_input"


def test_http_429_is_rate_limit():
    assert _classify_http_error(429, "Quota exceeded for quota metric") == "rate_limit"


def test_http_5xx_is_server():
    for s in (500, 502, 503, 504):
        assert _classify_http_error(s, "internal") == "server"


def test_http_unknown_falls_back():
    assert _classify_http_error(418, "i'm a teapot") == "unknown"
    assert _classify_http_error(0, "") == "unknown"


# ---- _classify_exception ----

def test_classify_http_error_exception():
    """An HttpError subclass should be classified by status."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    exc = HttpError(resp, b'{"error": "Not Found"}', uri="https://x/")
    kind, status = _classify_exception(exc)
    assert kind == "not_found"
    assert status == 404


def test_classify_value_error_is_bad_input():
    kind, status = _classify_exception(ValueError("bad arg"))
    assert kind == "bad_input"
    assert status == 0


def test_classify_file_not_found():
    kind, _ = _classify_exception(FileNotFoundError("/x/y"))
    assert kind == "not_found"


def test_classify_timeout_is_network():
    kind, _ = _classify_exception(TimeoutError("read timed out"))
    assert kind == "network"


def test_classify_permission_error():
    kind, _ = _classify_exception(PermissionError("denied"))
    assert kind == "permission"


def test_classify_unknown_generic():
    kind, _ = _classify_exception(RuntimeError("???"))
    assert kind == "unknown"


# ---- Wrapper end-to-end: synthetic tool raising HttpError ----

def test_wrap_for_sdk_returns_structured_error_payload():
    """When a registered tool raises HttpError 404, the wrapper should emit
    an is_error message whose JSON content carries _meta.error_kind=not_found.

    Locks down the legacy `_meta` envelope alongside the new RFC 9457 keys
    (those have their own dedicated tests below)."""
    import asyncio
    import json
    from googleapiclient.errors import HttpError
    from src.tools.registry import _wrap_for_sdk

    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"

    def boom(**kwargs):
        raise HttpError(resp, b'{"error": "Not Found"}', uri="https://x/")

    spec = {
        "name": "synthetic_fail",
        "fn": boom,
        "schema": {"description": "test", "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    # The @tool decorator stores call signature differently across versions;
    # invoke via .handler if needed, else __call__
    handler = getattr(wrapped, "handler", wrapped)
    result = asyncio.run(handler({}))
    assert result["is_error"] is True
    parsed = json.loads(result["content"][0]["text"])
    assert parsed["_meta"]["error_kind"] == "not_found"
    assert parsed["_meta"]["http_status"] == 404
    assert parsed["_meta"]["retryable"] is False
    assert parsed["_meta"]["exception_type"] == "HttpError"


# ---------- RFC 9457 problem+json envelope ----------

def _run_failing_tool(exc_factory):
    """Helper: build a synthetic spec whose fn raises, return parsed payload."""
    import asyncio
    import json
    from src.tools.registry import _wrap_for_sdk

    def boom(**kwargs):
        raise exc_factory()

    spec = {
        "name": "synth",
        "fn": boom,
        "schema": {"description": "x", "input_schema": {"type": "object", "properties": {}}},
    }
    wrapped = _wrap_for_sdk(spec)
    handler = getattr(wrapped, "handler", wrapped)
    result = asyncio.run(handler({}))
    return json.loads(result["content"][0]["text"]), result


def _httperr(status):
    from googleapiclient.errors import HttpError
    resp = MagicMock()
    resp.status = status
    resp.reason = "x"
    return HttpError(resp, b'{"error":"x"}', uri="https://x/")


def test_rfc9457_envelope_has_canonical_fields():
    """Top level must carry RFC 9457 canonical fields: type, title, status,
    detail, instance."""
    payload, _ = _run_failing_tool(lambda: _httperr(403))
    for key in ("type", "title", "status", "detail", "instance"):
        assert key in payload, f"missing canonical field {key}"
    assert payload["instance"] == "synth"
    assert payload["status"] == 403
    assert payload["type"].startswith("about:blank#")
    assert payload["_format"] == "application/problem+json"


def test_rfc9457_extensions_include_fix_hint_and_retry_after():
    """Extensions: error_kind, retriable, retry_after_ms, fix_hint,
    exception_type — must be present and useful."""
    payload, _ = _run_failing_tool(lambda: _httperr(429))
    assert payload["error_kind"] == "rate_limit"
    assert payload["retriable"] is True
    assert payload["retry_after_ms"] is not None
    assert payload["retry_after_ms"] > 0
    assert payload["fix_hint"]
    assert payload["exception_type"] == "HttpError"


def test_rfc9457_legacy_aliases_kept_for_backcompat():
    """The legacy `error` string and `_meta` envelope still ship so existing
    consumers (tests, system prompt rule 23 references) don't break."""
    payload, _ = _run_failing_tool(lambda: _httperr(404))
    assert "error" in payload  # legacy alias of `detail`
    assert payload["error"] == payload["detail"]
    assert "_meta" in payload  # legacy envelope
    assert payload["_meta"]["error_kind"] == payload["error_kind"]


def test_rfc9457_title_differs_per_error_kind():
    """auth_scope, permission, not_found, rate_limit must each get a distinct
    human-readable title — that's the whole point of a problem dict."""
    p_auth, _ = _run_failing_tool(lambda: _httperr(401))
    p_perm, _ = _run_failing_tool(lambda: _httperr(403))
    p_404, _ = _run_failing_tool(lambda: _httperr(404))
    p_rate, _ = _run_failing_tool(lambda: _httperr(429))
    titles = {p_auth["title"], p_perm["title"], p_404["title"], p_rate["title"]}
    assert len(titles) == 4, titles
