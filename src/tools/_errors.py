"""Exception classification for tool error reporting.

Lifted out of `registry.py` so other tool modules (notably
`apps_script_api.py`) can import the classifier without creating a
circular dependency on the registry's tool list. Pure functions, no
external dependencies beyond the optional `googleapiclient` import for
`HttpError` handling.
"""
from __future__ import annotations


def _classify_http_error(status: int, message: str) -> str:
    """Map Google API errors to a small set of `error_kind` labels the agent
    can switch on.

    Returns one of: auth_scope | permission | not_found | bad_input |
    rate_limit | server | network | unknown.

    Semantics:
      - auth_scope: token lacks the required OAuth scope (user needs re-OAuth)
      - permission: token is fine but lacks IAM/ACL access (different fix)
      - not_found: target doesn't exist; pick a different ID
      - bad_input: malformed request (range, body) — retry with fixed args
      - rate_limit: 429 quota; retry-after backoff
      - server: 5xx — retryable
      - network: connection/timeout — retryable
      - unknown: catch-all
    """
    msg = (message or "").lower()
    if status in (401,):
        return "auth_scope"
    if status == 403:
        if "insufficient_scope" in msg or "request had insufficient authentication scopes" in msg:
            return "auth_scope"
        return "permission"
    if status == 404:
        return "not_found"
    if status == 400:
        return "bad_input"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return "unknown"


class _IdempotencyConflict(Exception):
    """Raised internally when the same idempotency_key is reused with a
    different args hash. Classified as `idempotency_conflict`."""


def _classify_exception(exc: Exception) -> tuple[str, int]:
    """Determine error_kind + http_status (0 if N/A) from an exception."""
    # Lazy import — googleapiclient might not be installed in some test envs.
    try:
        from googleapiclient.errors import HttpError as _HttpError
    except Exception:
        _HttpError = None

    if _HttpError is not None and isinstance(exc, _HttpError):
        status = getattr(exc.resp, "status", 0) or 0
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        return _classify_http_error(status, str(exc)), status

    name = type(exc).__name__
    if name == "_IdempotencyConflict":
        return "idempotency_conflict", 0
    if name in {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError"}:
        return "network", 0
    if name == "FileNotFoundError":
        return "not_found", 0
    if name in {"ValueError", "TypeError", "KeyError"}:
        return "bad_input", 0
    if name == "PermissionError":
        return "permission", 0
    # requests library URL/scheme errors → bad_input (caller supplied a
    # malformed URL, not a network problem).
    if name in {"InvalidSchema", "InvalidURL", "MissingSchema", "URLRequired"}:
        return "bad_input", 0
    return "unknown", 0
