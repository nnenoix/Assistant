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

    @app.get(path)
    async def mcp_list_tools(request: Request) -> Response:
        """MCP discovery: GET /mcp returns the tool list as JSON-RPC."""
        from src.tools.registry import TOOLS

        # Optional auth — same Bearer/JWT contract as the rest of the app.
        if os.environ.get("ENABLE_MCP_HTTP_NOAUTH") != "1":
            from src.auth_oidc import verify_token
            authz = request.headers.get("authorization", "")
            if not authz.lower().startswith("bearer "):
                return JSONResponse(
                    {"error": "missing Bearer token"},
                    status_code=401,
                )
            check = verify_token(authz.split(None, 1)[1])
            if not check.get("ok"):
                return JSONResponse(
                    {"error": check.get("error", "invalid token")},
                    status_code=401,
                )
            request.state.user = check.get("claims")

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
        # Optional auth — same gate as discovery
        if os.environ.get("ENABLE_MCP_HTTP_NOAUTH") != "1":
            from src.auth_oidc import verify_token
            authz = request.headers.get("authorization", "")
            if not authz.lower().startswith("bearer "):
                return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32001, "message": "missing Bearer token"}}, status_code=401)
            check = verify_token(authz.split(None, 1)[1])
            if not check.get("ok"):
                return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32001, "message": check.get("error")}}, status_code=401)
            user = check.get("claims") or {}
            request.state.user = user
        else:
            user = {}

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

        # Optional RBAC gate — only when a user is bound and a tool has
        # a known policy_op.
        from src.tools.registry import TOOLS
        spec = next((t for t in TOOLS if t["name"] == bare_name), None)
        if spec is None:
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

        # Invoke via the same wrapper the in-process MCP server uses, so
        # idempotency / dry_run / truncation / problem-envelope all apply.
        from src.tools.registry import _wrap_for_sdk
        wrapped = _wrap_for_sdk(spec)
        handler = getattr(wrapped, "handler", wrapped)
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
