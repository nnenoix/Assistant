"""Unit tests for `src/tools/_retry.py` — the unified HTTP retry wrapper
for non-Google, non-WB calls (web fetch, fx_rate, etc.).

We don't try to exercise the real urllib3 retry machinery here — that's
pre-validated by urllib3's own test suite. Instead we lock down:
  - The Retry policy carries the expected codes / backoff / cap
  - `retrying_request` builds and reuses a single session
  - Callers in `web.py` / `external.py` actually call through the wrapper
"""
from unittest.mock import patch

import pytest

from src.tools import _retry


def test_retry_policy_includes_standard_transient_codes():
    """429 + 5xx are the canonical retry set. Other 4xx must NOT retry —
    those are caller bugs (bad input / auth) the agent should surface."""
    policy = _retry._build_retry()
    expected = {429, 500, 502, 503, 504}
    assert set(policy.status_forcelist) == expected


def test_retry_policy_respects_retry_after_header():
    """Servers honor `Retry-After`; we must too — otherwise we hammer them
    and earn the 12h ban (esp. WB)."""
    policy = _retry._build_retry()
    assert policy.respect_retry_after_header is True


def test_retry_policy_caps_backoff_at_64_seconds():
    """Google's recommended formula has max_backoff=64s. Without a cap a
    5th retry could sleep 32+ seconds and still be in the loop."""
    policy = _retry._build_retry()
    assert policy.backoff_max == _retry.DEFAULT_MAX_BACKOFF_S
    assert policy.backoff_max <= 64.0


def test_retry_policy_total_attempts_matches_google_guide():
    """5 retries = ~62s worst-case sleep before failing. That matches the
    audit's reference to Google's exponential-backoff guide."""
    policy = _retry._build_retry()
    assert policy.total == _retry.DEFAULT_TOTAL == 5


def test_retrying_request_uses_shared_session():
    """`_default_session` should be created once and reused across calls.
    Prevents leaking connections per call."""
    # Reset to force build
    _retry._default_session = None

    sentinel_response = object()
    with patch.object(_retry, "make_retrying_session") as mock_make:
        from unittest.mock import MagicMock
        mock_session = MagicMock()
        mock_session.request.return_value = sentinel_response
        mock_make.return_value = mock_session

        r1 = _retry.retrying_request("GET", "http://x/")
        r2 = _retry.retrying_request("GET", "http://x/")

        assert r1 is sentinel_response
        assert r2 is sentinel_response
        # Built exactly once
        assert mock_make.call_count == 1
        # But two requests went through it
        assert mock_session.request.call_count == 2


def test_make_retrying_session_mounts_adapter_for_both_schemes():
    sess = _retry.make_retrying_session()
    assert "http://" in sess.adapters
    assert "https://" in sess.adapters


# ---------- caller integration ----------

def test_web_fetch_uses_retrying_wrapper():
    """`web.fetch` must route through `retrying_request`, not bare `requests.get`."""
    from src.tools import web
    from unittest.mock import MagicMock

    fake_resp = MagicMock()
    fake_resp.iter_content.return_value = [b"hello"]
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "text/plain"}
    fake_resp.url = "http://x/"
    fake_resp.text = "hello"

    with patch.object(web, "retrying_request", return_value=fake_resp) as mock_req:
        result = web.fetch("http://x/", mode="text")
    mock_req.assert_called_once()
    assert mock_req.call_args.args[0] == "GET"
    assert result["_meta"]["status_code"] == 200


def test_external_fx_rate_uses_retrying_wrapper():
    """`external.fx_rate` must route through `retrying_request`."""
    from src.tools import external
    from unittest.mock import MagicMock

    xml = (
        b"<?xml version='1.0' encoding='windows-1251'?><ValCurs>"
        b"<Valute><CharCode>USD</CharCode><Nominal>1</Nominal>"
        b"<Value>92,5</Value><Name>USD</Name></Valute></ValCurs>"
    )
    fake_resp = MagicMock()
    fake_resp.content = xml
    fake_resp.raise_for_status.return_value = None

    with patch.object(external, "retrying_request", return_value=fake_resp) as mock_req:
        result = external.fx_rate("USD", date_iso="2026-05-22")
    mock_req.assert_called_once()
    assert mock_req.call_args.args[0] == "GET"
    assert result["rate_to_rub"] == 92.5
