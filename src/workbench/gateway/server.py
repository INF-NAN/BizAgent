"""The gateway as an MCP server (Streamable HTTP).

Clients connect to ``/mcp`` and identify their session with the ``X-Workbench-Session``
header. ``list_tools`` returns the session's allowlisted tools with ``<scenario>__`` prefixes;
``call_tool`` goes through Gateway.call_tool (policy, rate limit, audit, normalization).
Write/destructive tools need a one-time token in ``X-Approval-Token`` issued by the
application layer (``POST /admin/approvals`` here, or the API's approval flow).

MCP SDK 1.26.0 facts used (docs/RECON.md): lowlevel ``Server.call_tool(validate_input=...)``
(mcp/server/lowlevel/server.py:492) accepts a returned ``CallToolResult`` as-is (:539-540);
``server.request_context.request`` carries the HTTP request (:758);
``StreamableHTTPSessionManager(app, json_response, stateless)`` (streamable_http_manager.py:60-65).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from workbench.gateway.core import Gateway, UnknownSessionError

SESSION_HEADER = "x-workbench-session"
APPROVAL_HEADER = "x-approval-token"
TRACE_HEADER = "x-trace-id"


def _header(server: Server[Any, Any], name: str) -> str | None:
    request = server.request_context.request
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def build_mcp_server(gateway: Gateway) -> Server[Any, Any]:
    server: Server[Any, Any] = Server("workbench-gateway")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        sid = _header(server, SESSION_HEADER)
        if not sid:
            raise ValueError(f"missing {SESSION_HEADER} header")
        return [
            types.Tool(
                name=t.name, description=f"[risk:{t.risk}] {t.description}", inputSchema=t.input_schema
            )
            for t in gateway.list_tools(sid)
        ]

    # validate_input=False: argument errors are normalized by the gateway (with hints)
    # instead of the SDK's generic jsonschema message.
    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        sid = _header(server, SESSION_HEADER)
        if not sid:
            raise ValueError(f"missing {SESSION_HEADER} header")
        outcome = await gateway.call_tool(
            sid,
            name,
            arguments or {},
            approval_token=_header(server, APPROVAL_HEADER),
            trace_id=_header(server, TRACE_HEADER),
        )
        payload = outcome.as_dict()
        is_error = outcome.status in ("error", "denied")
        text = json.dumps(payload, ensure_ascii=False) if is_error else outcome.text
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent={"status": outcome.status, "trace_id": outcome.trace_id},
            isError=is_error,
        )

    return server


class RegisterRequest(BaseModel):
    session_id: str
    scenario: str
    url: str
    allowlist: list[str] | None = None


class ApprovalRequest(BaseModel):
    session_id: str
    tool: str
    arguments: dict[str, Any] = {}
    approver: str


def create_gateway_app(gateway: Gateway) -> Starlette:
    server = build_mcp_server(gateway)
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    class _McpEndpoint:
        # A class instance (not a function) makes Starlette's Route treat it as a raw ASGI app,
        # so "/mcp" is served exactly (a Mount would 307-redirect "/mcp" to "/mcp/").
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

    async def register(request: Request) -> JSONResponse:
        req = RegisterRequest.model_validate(await request.json())
        route = await gateway.register_session(req.session_id, req.scenario, req.url, req.allowlist)
        return JSONResponse({"session_id": route.session_id, "tools": sorted(route.allowlist)})

    async def approve(request: Request) -> JSONResponse:
        req = ApprovalRequest.model_validate(await request.json())
        try:
            token = gateway.issue_approval(req.session_id, req.tool, req.arguments, req.approver)
        except UnknownSessionError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse({"token": token})

    async def risk(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        try:
            return JSONResponse(gateway.risk_table(sid))
        except UnknownSessionError:
            return JSONResponse({"error": "unknown session"}, status_code=404)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=_McpEndpoint(), methods=["GET", "POST", "DELETE"]),
            Route("/admin/sessions", register, methods=["POST"]),
            Route("/admin/approvals", approve, methods=["POST"]),
            Route("/admin/risk/{sid}", risk, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    # When mounted inside another app (the API), the parent must run this manager itself:
    # Starlette does not run lifespans of mounted sub-apps.
    app.state.session_manager = manager
    return app
