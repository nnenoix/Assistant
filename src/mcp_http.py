"""MCP Streamable HTTP transport for the SDK MCP server.

Exposes the registered tools to external MCP clients (LibreChat, Open
WebUI, VSCode Copilot Chat with MCP plugin) over HTTPS. This is what
turns the desktop-app's in-process MCP server into a multi-user service.

Mounting:
    from src.mcp_http import mount_mcp_http
    mount_mcp_http(app)  # app: FastAPI

Then start the server normally — MCP clients connect to
`http(s)://<host>:<port>/mcp` (POST for tool calls, GET with
`Accept: text/event-stream` for the streamable channel).

Auth: requires a Bearer JWT (verified via `src.auth_oidc.verify_token`)
unless `ENABLE_MCP_HTTP_NOAUTH=1` (DEV-ONLY).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


def _authenticate(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Resolve the bearer token to a claims dict, or return a 401 response.

    Returns (user_claims, None) on success, (None, error_response) on
    failure, or ({}, None) when auth is disabled (DEV).

    Refuses tokens that came back with `unsafe_no_verify=True` unless
    the operator explicitly opted in via `ALLOW_UNSAFE_OIDC=1`. Without
    this guard, a missing `python-jose` install would silently accept
    arbitrary JWTs.
    """
    if os.environ.get("ENABLE_MCP_HTTP_NOAUTH") == "1":
        return {}, None
    from src.auth_oidc import verify_token
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        return None, JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "missing Bearer token"}},
            status_code=401,
        )
    # `split(None, 1)` collapses whitespace; if the caller sent literally
    # "Bearer " (no token) the index lookup would raise IndexError and
    # FastAPI would 500. Per RFC 6750 we MUST return 401.
    parts = authz.split(None, 1)
    token = parts[1] if len(parts) > 1 else ""
    check = verify_token(token)
    if not check.get("ok"):
        return None, JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": check.get("error", "invalid token")}},
            status_code=401,
        )
    if check.get("unsafe_no_verify") and os.environ.get("ALLOW_UNSAFE_OIDC") != "1":
        return None, JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "OIDC verification unavailable (install python-jose) — refusing token. Set ALLOW_UNSAFE_OIDC=1 to bypass (DEV-ONLY)."}},
            status_code=401,
        )
    return check.get("claims") or {}, None


def mount_mcp_http(app, *, path: str = "/mcp") -> None:
    """Attach MCP Streamable HTTP endpoint to a FastAPI app.

    Returns None. Uses lazy imports so missing `mcp` / starlette extras
    don't crash the rest of the app at boot — instead we log a warning
    and the /mcp path returns 503 when called.

    Phase 0 scaffold: this stub serves the tool LIST via /mcp; the real
    Streamable HTTP protocol (POST /mcp with JSON-RPC envelope, SSE for
    server→client streaming) is added incrementally. Real clients can
    already discover tools — invocation comes next.
    """
    enable = os.environ.get("ENABLE_MCP_HTTP", "0") == "1"
    if not enable:
        logger.info("MCP HTTP disabled (set ENABLE_MCP_HTTP=1 to enable)")
        return

    # Hard refusal: ENABLE_MCP_HTTP_NOAUTH=1 disables both Bearer + RBAC.
    # If the operator ALSO claims to be running in prod, this is almost
    # certainly a misconfiguration and would expose every tool unauthenticated.
    # We refuse to mount rather than silently exposing the surface.
    if os.environ.get("ENABLE_MCP_HTTP_NOAUTH") == "1":
        app_env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "").lower()
        if app_env in {"prod", "production"}:
            raise RuntimeError(
                "REFUSING to mount MCP HTTP: ENABLE_MCP_HTTP_NOAUTH=1 with "
                f"APP_ENV/ENV={app_env!r}. The noauth flag bypasses OIDC and "
                "RBAC for every request — never enable it in production. "
                "Unset one of the two env vars to proceed."
            )
        logger.warning(
            "MCP HTTP mounting WITHOUT authentication (ENABLE_MCP_HTTP_NOAUTH=1). "
            "All RBAC checks will be skipped. DEV-ONLY."
        )

    # Pre-wrap every tool ONCE at mount time so POST /mcp doesn't rebuild
    # the @tool closure (with idempotency / dry_run / OTel / quota /
    # truncation middleware) on every request. The wrapped handler is
    # identical to the one the in-process MCP server uses.
    from src.tools.registry import TOOLS, _wrap_for_sdk
    _HANDLERS: dict[str, Any] = {}
    _SPECS: dict[str, dict] = {}
    for _spec in TOOLS:
        _wrapped = _wrap_for_sdk(_spec)
        _HANDLERS[_spec["name"]] = getattr(_wrapped, "handler", _wrapped)
        _SPECS[_spec["name"]] = _spec

    @app.get(path)
    async def mcp_list_tools(request: Request) -> Response:
        """MCP discovery: GET /mcp returns the tool list as JSON-RPC."""
        from src.tools.registry import TOOLS

        user, err = _authenticate(request)
        if err is not None:
            return err
        if user:
            request.state.user = user

        tool_list = [
            {
                "name": f"mcp__gworkagent__{t['name']}",
                "description": t["schema"]["description"][:300],
                "inputSchema": t["schema"]["input_schema"],
                **(
                    {"annotations": {
                        "readOnlyHint": t["annotations"].readOnlyHint,
                        "destructiveHint": t["annotations"].destructiveHint,
                        "idempotentHint": t["annotations"].idempotentHint,
                        "openWorldHint": t["annotations"].openWorldHint,
                    }}
                    if t.get("annotations") else {}
                ),
            }
            for t in TOOLS
        ]
        return JSONResponse({
            "jsonrpc": "2.0",
            "result": {"tools": tool_list, "count": len(tool_list)},
        })

    @app.post(path)
    async def mcp_invoke(request: Request) -> Response:
        """MCP invocation: POST /mcp with JSON-RPC envelope to call a tool.

        Body shape (MCP `tools/call`):
            {"jsonrpc":"2.0","id":<n>,"method":"tools/call",
             "params":{"name":"mcp__gworkagent__<tool>","arguments":{...}}}

        Returns the wrapped tool's content (text/error) inside a JSON-RPC
        success envelope. Per-tool annotations + idempotency + dry_run +
        truncation still apply (we route through the SAME wrapper the
        in-process MCP server uses)."""
        user, err = _authenticate(request)
        if err is not None:
            return err
        if user:
            request.state.user = user

        try:
            envelope = await request.json()
        except Exception as e:
            return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": f"parse error: {e}"}}, status_code=400)

        method = envelope.get("method", "")
        params = envelope.get("params", {}) or {}
        req_id = envelope.get("id")

        if method != "tools/call":
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}; supported: tools/call"},
            }, status_code=400)

        tool_name = params.get("name", "")
        # MCP clients prefix with `mcp__<server>__`. Strip if present.
        bare_name = tool_name.split("__")[-1] if "__" in tool_name else tool_name
        arguments = params.get("arguments", {}) or {}

        spec = _SPECS.get(bare_name)
        handler = _HANDLERS.get(bare_name)
        if spec is None or handler is None:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"tool {bare_name!r} not found"},
            }, status_code=404)

        if user:
            from src.rbac import check_permission
            allowed = check_permission(user.get("groups") or [], spec.get("policy_op") or "")
            if not allowed["allowed"]:
                return JSONResponse({
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32002, "message": f"forbidden by RBAC: {allowed['reason']}"},
                }, status_code=403)

        try:
            tool_result = await handler(arguments)
        except Exception as e:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": f"internal: {type(e).__name__}: {str(e)[:300]}"},
            }, status_code=500)

        # Translate the SDK result (`{content: [...], is_error?: True}`) into
        # the MCP `tools/call` response shape.
        is_err = bool(tool_result.get("is_error"))
        body = {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": tool_result.get("content", []), "isError": is_err},
        }
        return JSONResponse(body, status_code=200)

    logger.info(f"MCP HTTP transport mounted at {path}")
