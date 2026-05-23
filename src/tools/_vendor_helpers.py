"""Cross-vendor helpers: OAuth token refresh, session cookie persistence,
200-but-error response parsing. Used by Avito / Yandex / 1С wrappers.

State storage: `.data/vendor_tokens/<key>.json` — small files keyed by
account+vendor so multiple sellers/accounts can coexist.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.tools._safe_id import is_safe_id


_TOKENS_DIR = DATA_DIR / "vendor_tokens"
_TOKENS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_cache_key(vendor: str, account_key: str) -> bool:
    """Cache filenames concat `vendor` + `account_key` — both go straight
    into the path. Both halves must satisfy the same safe-id contract
    used by MDM table names and the migration scanner."""
    return is_safe_id(vendor) and is_safe_id(account_key)


# ============================================================
# OAuth2 client_credentials refresh (Avito / Yandex / generic)
# ============================================================

def get_cached_oauth_token(vendor: str, account_key: str,
                          fetch_fn,
                          refresh_skew_s: int = 60) -> dict:
    """Return a cached OAuth token, refreshing if expired or within
    `refresh_skew_s` of expiry. `fetch_fn()` must return
    `{ok, data: {access_token, expires_in}}` — same shape as the existing
    vendor `*_auth` functions.

    Stored at `.data/vendor_tokens/<vendor>__<account_key>.json` with
    `{access_token, fetched_at, expires_at}`. `vendor` and `account_key`
    are validated against `[A-Za-z0-9_-]{1,64}` so neither can carry a
    path-traversal payload into the on-disk filename."""
    if not _safe_cache_key(vendor, account_key):
        # Bypass the cache entirely on a malformed key — fetch fresh and
        # return that to the caller without persisting. Failing closed is
        # safer than failing the auth call.
        return fetch_fn()
    path = _TOKENS_DIR / f"{vendor}__{account_key}.json"
    now = time.time()
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("expires_at", 0) - now > refresh_skew_s:
                return {"ok": True, "data": {"access_token": cached["access_token"]},
                        "_meta": {"cached": True,
                                  "expires_in_s": int(cached["expires_at"] - now)}}
        except Exception:
            pass
    # Fetch fresh
    fresh = fetch_fn()
    if not fresh.get("ok"):
        return fresh
    tok = fresh["data"].get("access_token")
    expires_in = fresh["data"].get("expires_in", 3600)
    if tok:
        path.write_text(json.dumps({
            "access_token": tok,
            "fetched_at": now,
            "expires_at": now + expires_in,
        }), encoding="utf-8")
    return {**fresh, "_meta": {**(fresh.get("_meta") or {}), "cached": False}}


def invalidate_oauth_cache(vendor: str, account_key: str) -> dict:
    """Drop a cached token (e.g. when the vendor returns 401 unexpectedly)."""
    if not _safe_cache_key(vendor, account_key):
        return {"ok": False, "error_kind": "bad_input",
                "error": f"invalid cache key {vendor!r}/{account_key!r}"}
    path = _TOKENS_DIR / f"{vendor}__{account_key}.json"
    if path.exists():
        path.unlink()
        return {"ok": True, "data": {"removed": str(path)}}
    return {"ok": True, "data": {"removed": None}}


# ============================================================
# 1С session cookie persistence
# ============================================================
# 1С OData often (configurably) returns Set-Cookie on first Basic Auth call
# and accepts the cookie on subsequent calls — faster than re-auth every
# request. We store ONE cookie per (base_url, login) pair.

_SESSIONS_DIR = DATA_DIR / "vendor_sessions"
_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_key(base_url: str, login: str) -> str:
    import hashlib
    return hashlib.sha256(f"{base_url}::{login}".encode("utf-8")).hexdigest()[:24]


def get_onec_session_cookie(base_url: str, login: str) -> str | None:
    """Return cached 1С session cookie or None."""
    path = _SESSIONS_DIR / f"onec_{_session_key(base_url, login)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("expires_at", 0) < time.time():
        return None
    return data.get("cookie")


def save_onec_session_cookie(base_url: str, login: str, cookie: str,
                             ttl_s: int = 1800) -> None:
    """Cache a 1С session cookie (default 30 min — matches typical session
    timeout in 1С Bus)."""
    path = _SESSIONS_DIR / f"onec_{_session_key(base_url, login)}.json"
    path.write_text(json.dumps({
        "cookie": cookie,
        "stored_at": time.time(),
        "expires_at": time.time() + ttl_s,
    }), encoding="utf-8")


def invalidate_onec_session(base_url: str, login: str) -> None:
    path = _SESSIONS_DIR / f"onec_{_session_key(base_url, login)}.json"
    if path.exists():
        path.unlink()


# ============================================================
# Per-vendor 200-but-error parser
# ============================================================
# Some vendors (notably WB) return HTTP 200 with `{"errors": [...]}` in the
# body — must be treated as failure even though the status was 200. This
# helper inspects a parsed JSON body and returns (is_error, error_msg).

def detect_200_error(vendor: str, body: dict | list) -> tuple[bool, str | None]:
    """Inspect a parsed-JSON response body for vendor-specific failure
    indicators that don't surface in the HTTP status."""
    if not isinstance(body, (dict, list)):
        return False, None
    # WB: {"errors": ["message"]} or {"error": "msg"} on 200
    if isinstance(body, dict):
        errs = body.get("errors") or body.get("error")
        if errs:
            if isinstance(errs, list):
                return True, "; ".join(str(e) for e in errs)[:300]
            return True, str(errs)[:300]
        # Ozon: top-level `code` with a non-0 / non-OK value
        if vendor == "ozon" and body.get("code") not in (None, 0, "0"):
            return True, f"ozon code={body.get('code')} msg={body.get('message', '')[:200]}"
        # YaMarket: `status: ERROR` envelope
        if vendor == "yamarket" and body.get("status") == "ERROR":
            return True, str(body.get("errors") or body.get("description"))[:300]
        # МойСклад: returns errors array even on 200 sometimes
        if vendor == "moysklad" and "errors" in body:
            return True, json.dumps(body["errors"])[:300]
        # Boxberry: list with `{"err":"..."}` first element
        if isinstance(body, list) and body and isinstance(body[0], dict) and "err" in body[0]:
            return True, body[0]["err"]
    # Boxberry list-error path
    if isinstance(body, list) and body and isinstance(body[0], dict):
        if "err" in body[0]:
            return True, body[0]["err"]
    return False, None
