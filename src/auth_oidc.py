"""OIDC verifier (Authentik / Keycloak / any OIDC IdP).

Phase 0 multi-user authentication: FastAPI middleware extracts a
`Authorization: Bearer <jwt>` header, fetches the IdP's JWKs (cached for
TTL), verifies signature + expiry + audience, exposes `request.state.user`
to handlers.

Default issuer = local Authentik via docker-compose. Override via env
`OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL`.

Stub-grade: doesn't yet handle PKCE flows, refresh tokens, or
multi-issuer federation. Production wires Authentik's flows fully.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any


OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:9000/application/o/workspace-agent/")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "workspace-agent")
OIDC_JWKS_URL = os.environ.get(
    "OIDC_JWKS_URL",
    OIDC_ISSUER.rstrip("/") + "/jwks/",
)

_JWKS_CACHE: dict[str, Any] = {"fetched_at": 0, "keys": None}
_JWKS_TTL_S = 3600


def _fetch_jwks() -> dict | None:
    """Fetch + cache the IdP's JWK set."""
    now = time.time()
    if _JWKS_CACHE["keys"] and now - _JWKS_CACHE["fetched_at"] < _JWKS_TTL_S:
        return _JWKS_CACHE["keys"]
    try:
        with urllib.request.urlopen(OIDC_JWKS_URL, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _JWKS_CACHE["keys"] = data
        _JWKS_CACHE["fetched_at"] = now
        return data
    except Exception:
        return None


def verify_token(token: str) -> dict:
    """Verify a JWT, return claims dict.

    Returns:
        {"ok": True, "claims": {sub, email, groups, ...}}
        OR {"ok": False, "error": "..."}

    Phase 0 scaffold: uses python-jose if available. If not, parses
    claims WITHOUT signature verification (for local dev only — clearly
    UNSAFE in production; the function returns `"unsafe_no_verify": True`
    so callers can refuse in prod mode).
    """
    if not token:
        return {"ok": False, "error": "empty token"}
    try:
        from jose import jwt, jwk
        from jose.utils import base64url_decode
    except ImportError:
        # Dev mode: decode WITHOUT verifying signature.
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return {"ok": False, "error": "malformed JWT"}
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"decode failed: {e}"}
        return {"ok": True, "claims": claims, "unsafe_no_verify": True}

    jwks = _fetch_jwks()
    if not jwks:
        return {"ok": False, "error": f"jwks fetch failed: {OIDC_JWKS_URL}"}
    try:
        headers = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == headers["kid"]), None)
        if not key:
            return {"ok": False, "error": f"unknown kid {headers.get('kid')}"}
        claims = jwt.decode(
            token, key, algorithms=[key.get("alg", "RS256")],
            audience=OIDC_AUDIENCE, issuer=OIDC_ISSUER,
        )
        return {"ok": True, "claims": claims}
    except Exception as e:
        return {"ok": False, "error": f"verify failed: {type(e).__name__}: {e}"}


def user_from_claims(claims: dict) -> dict:
    """Normalise IdP-specific claim shapes into a uniform user dict.
    Common fields: sub, email, name, groups, tenant."""
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("preferred_username"),
        "groups": claims.get("groups") or claims.get("realm_access", {}).get("roles") or [],
        "tenant": claims.get("tenant") or claims.get("tid") or "default",
    }
