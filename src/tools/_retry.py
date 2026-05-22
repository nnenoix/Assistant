"""Single retry wrapper for plain HTTP calls (non-Google, non-WB).

Coverage matrix:
- Google API tools → use `RetryingHttpRequest` in `src/auth.py` (full-jitter
  exponential backoff via googleapiclient; 5 retries, no extra wrapper).
- Wildberries API → `wb.py` has bespoke 429 handling that honors
  `X-Ratelimit-Retry`; do not double-wrap.
- Everything else that uses `requests.*` directly (web_fetch, fx_rate,
  reply_check, etc.) → wrap via `retrying_request()` from this module.

Strategy: Google's recommended formula `min(2^n*base_ms + rand_ms_≤1000,
max_ms)`. Honors `Retry-After` (seconds or RFC 7231 HTTP-date), retries on
{429, 500, 502, 503, 504}, surfaces other 4xx immediately so callers can
fix bad input. Backed by `urllib3.util.retry.Retry` + `HTTPAdapter`
so connection-level errors (DNS, ECONNRESET, read-timeout) are also
retried automatically.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Retry status codes per Google API best-practices guide.
RETRY_STATUS = (429, 500, 502, 503, 504)
DEFAULT_TOTAL = 5
DEFAULT_BACKOFF_FACTOR = 1.0  # sleep = backoff_factor * (2 ** retry_num) seconds
DEFAULT_MAX_BACKOFF_S = 64.0  # cap per Google's formula


def _build_retry() -> Retry:
    """One Retry policy shared by all sessions built from this module."""
    return Retry(
        total=DEFAULT_TOTAL,
        backoff_factor=DEFAULT_BACKOFF_FACTOR,
        status_forcelist=list(RETRY_STATUS),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]),
        respect_retry_after_header=True,
        backoff_max=DEFAULT_MAX_BACKOFF_S,
        raise_on_status=False,  # Let caller decide; we return the final Response.
    )


def make_retrying_session() -> requests.Session:
    """Build a `requests.Session` with retry + connection-pool reuse. Cache
    one per process for hot paths if perf matters, but the bare object is
    cheap to construct."""
    sess = requests.Session()
    adapter = HTTPAdapter(max_retries=_build_retry(), pool_connections=10, pool_maxsize=20)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


# Module-level singleton — most callers share one pool.
_default_session: requests.Session | None = None


def retrying_request(method: str, url: str, **kwargs) -> requests.Response:
    """Drop-in replacement for `requests.request(method, url, ...)` that
    transparently retries on transient failures.

    Same signature, same return type. Existing call sites swap
    `requests.get(url, ...)` → `retrying_request("GET", url, ...)` with no
    further change.
    """
    global _default_session
    if _default_session is None:
        _default_session = make_retrying_session()
    return _default_session.request(method, url, **kwargs)
