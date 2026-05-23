"""Shared JSON-over-HTTP helpers for vendor clients.

Multiple vendor modules (`edo.py`, `social.py`) shipped byte-identical
`_get_json` / `_post_json` helpers — same urllib boilerplate, same
`{ok, data?, error?, _meta}` shape, same 300-char truncation. This
module is the one source of truth.

Why not also fold in `wb.py`, `ozon.py`, `payments.py`, etc.: each
adds its own irreducible spice (host map + JWT retry, Client-Id +
Api-Key headers, Tinkoff SHA-256 token, ЮKassa Basic auth, 200-but-
error body parsing, etc.) — collapsing them into one helper would
either grow the parameter list past the point of clarity, or push
that spice into a chain of optional kwargs the IDE can't help with.

Standard envelope:
    on success:  {"ok": True,  "data": <parsed_json>,
                  "_meta": {"http_status": 200}}
    on 4xx/5xx:  {"ok": False, "error": <truncated body>,
                  "error_kind": "permission" | "not_found" | "rate_limit"
                                | "server" | "bad_input",
                  "_meta": {"http_status": <code>}}

`error_kind` matches the classifier shape the rest of the codebase
uses (`src.tools._errors._classify_http_error`) so the agent sees the
same vocabulary across every vendor.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.tools._errors import _classify_http_error


def _classify(code: int, message: str = "") -> str:
    """HTTP status code → error_kind label.

    Delegates to `src.tools._errors._classify_http_error` so vendor
    responses share the exact vocabulary used by Google API errors and
    `_wrap_for_sdk`'s problem-envelope path. The earlier inline copy
    diverged in subtle ways (401 → "permission" instead of the project's
    "auth_scope"; 422 → "bad_input" vs. "unknown") — those drifts are
    now eliminated."""
    return _classify_http_error(code, message)


def get_json(url: str, headers: dict | None = None, timeout: int = 60) -> dict:
    """GET `url`, return the standard envelope. Body truncated to 300
    chars on error so a stray multi-MB HTML error page doesn't blow
    out the LLM context."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "ok": True,
                "data": json.loads(resp.read().decode("utf-8")),
                "_meta": {"http_status": resp.status},
            }
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", errors="replace")
        return {
            "ok": False,
            "error": body,
            # Pass body so 403+`insufficient_scope` correctly routes to
            # `auth_scope` rather than the generic `permission` label.
            "error_kind": _classify(e.code, body),
            "_meta": {"http_status": e.code},
        }


def post_json(url: str, body: dict, headers: dict | None = None,
              timeout: int = 60) -> dict:
    """POST `body` as JSON to `url`, return the standard envelope.
    Content-Type + Accept JSON headers are merged with any caller-
    supplied headers (caller wins on duplicate keys)."""
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **(headers or {}),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=h,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "ok": True,
                "data": json.loads(resp.read().decode("utf-8")),
                "_meta": {"http_status": resp.status},
            }
    except urllib.error.HTTPError as e:
        err_body = e.read()[:300].decode("utf-8", errors="replace")
        return {
            "ok": False,
            "error": err_body,
            "error_kind": _classify(e.code, err_body),
            "_meta": {"http_status": e.code},
        }
