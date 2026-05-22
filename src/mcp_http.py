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

        Phase 0 scaffold: returns 501 with a clear next-step note. The real
        protocol wiring (route to `_wrap_for_sdk`'s wrapped handlers,
        translate result back into MCP content blocks) is the next
        increment.
        """
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": "Tool invocation over HTTP not yet wired. Phase 0 scaffold returns discovery only. Use the desktop app for actual tool calls until this lands.",
                },
            },
            status_code=501,
        )

    logger.info(f"MCP HTTP transport mounted at {path}")
