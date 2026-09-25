"""The gateway as an MCP server (Streamable HTTP).

Clients connect to ``/mcp`` and identify their session with the ``X-Workbench-Session``
header. ``list_tools`` returns the session's allowlisted tools with ``<scenario>__`` prefixes;
``call_tool`` goes through Gateway.call_tool (policy, rate limit, audit, normalization).
Calls the approval policy sends to a person (write and destructive, unless a rule says otherwise;
ADR-030) need a one-time token in ``X-Approval-Token`` issued by the application layer
(``POST /admin/approvals`` here, or the API's approval flow); for an ``auto_approve`` rule the
gateway issues the token itself. Every human token is
bound to a preview (ADR-029): ``POST /admin/previews`` runs one and returns its record, and
``/admin/approvals`` takes its ``preview_id`` or runs the preview itself before issuing, and
refuses (409) a call the approval policy denies (ADR-030).

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
from workbench.gateway.policy import ApprovalError

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
    # tool -> HTTP method from the offline catalog (risk floor, ADR-015); omit if unknown
    tool_methods: dict[str, str] = {}


class PreviewRequest(BaseModel):
    session_id: str
    tool: str
    arguments: dict[str, Any] = {}


class ApprovalRequest(BaseModel):
    session_id: str
    tool: str
    arguments: dict[str, Any] = {}
    approver: str
    # a preview from POST /admin/previews; without it the preview runs here, before the token
    preview_id: str | None = None


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
        route = await gateway.register_session(
            req.session_id, req.scenario, req.url, req.allowlist, tool_methods=req.tool_methods
        )
        return JSONResponse({"session_id": route.session_id, "tools": sorted(route.allowlist)})

    async def preview(request: Request) -> JSONResponse:
        req = PreviewRequest.model_validate(await request.json())
        try:
            record = await gateway.preview(req.session_id, req.tool, req.arguments)
        except UnknownSessionError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(record)

    async def approve(request: Request) -> JSONResponse:
        req = ApprovalRequest.model_validate(await request.json())
        record = None
        try:
            verdict = gateway.approval_verdict(req.session_id, req.tool, req.arguments)
            if verdict.decision == "deny":  # nobody can approve it, so do not run a preview (ADR-030)
                reason = f"the approval policy refuses this call ({verdict.reason}); nobody can approve it"
                return JSONResponse({"error": reason, "policy": verdict.as_dict()}, status_code=409)
            record = (
                None if req.preview_id else await gateway.preview(req.session_id, req.tool, req.arguments)
            )
            token = gateway.issue_approval(
                req.session_id,
                req.tool,
                req.arguments,
                req.approver,
                preview=record,
                preview_id=req.preview_id,
            )
        except UnknownSessionError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ApprovalError as exc:  # a required preview failed: only a rejection is possible
            return JSONResponse({"error": str(exc), "preview": record}, status_code=409)
        return JSONResponse({"token": token, "preview": record})

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
            Route("/admin/previews", preview, methods=["POST"]),
            Route("/admin/approvals", approve, methods=["POST"]),
            Route("/admin/risk/{sid}", risk, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    # When mounted inside another app (the API), the parent must run this manager itself:
    # Starlette does not run lifespans of mounted sub-apps.
    app.state.session_manager = manager
    return app
