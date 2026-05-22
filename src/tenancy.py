"""Tenant-id propagation middleware.

Reads `tenant_id` from one of (in order):
  1. The verified OIDC claim `tenant` / `tid` (`src.auth_oidc.user_from_claims`)
  2. `X-Tenant-Id` request header (for ServiceAccount / S2S callers)
  3. `tenant_id` query parameter (least preferred, dev convenience)
  4. Defaults to `"default"`

Binds the value to `request.state.tenant_id`. Downstream code (tools that
write to Postgres / audit log) uses `current_tenant_id()` to fetch it.

Wired into the FastAPI app via `add_tenant_middleware(app)`.
"""
from __future__ import annotations

import contextvars
from typing import Any

_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tenant", default="default"
)


def current_tenant_id() -> str:
    """Return the tenant_id bound to the current request context (or 'default').

    Use inside tool implementations when writing to multi-tenant storage:
        from src.tenancy import current_tenant_id
        tenant = current_tenant_id()
        db.execute("INSERT INTO audit_log (tenant_id, ...) VALUES (?, ...)", tenant, ...)
    """
    return _current_tenant.get()


def add_tenant_middleware(app) -> None:
    """Install the middleware. Idempotent — calls past the first are no-ops."""
    from fastapi import Request

    if getattr(app, "_tenant_middleware_added", False):
        return
    app._tenant_middleware_added = True

    @app.middleware("http")
    async def _tenant_mw(request: Request, call_next):
        # 1. From OIDC claims (set by an earlier auth middleware, if any)
        user = getattr(request.state, "user", None) or {}
        tenant = user.get("tenant")
        # 2. Header (S2S clients)
        if not tenant:
            tenant = request.headers.get("x-tenant-id")
        # 3. Query param (dev)
        if not tenant:
            tenant = request.query_params.get("tenant_id")
        # 4. Default
        tenant = tenant or "default"

        token = _current_tenant.set(tenant)
        try:
            request.state.tenant_id = tenant
            response = await call_next(request)
            response.headers["X-Tenant-Id"] = tenant
            return response
        finally:
            _current_tenant.reset(token)
