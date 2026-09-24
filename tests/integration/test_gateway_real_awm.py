"""Gateway end-to-end over MCP: real AWM env <- gateway MCP server <- MCP client."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from workbench.config import GatewaySettings
from workbench.envs.manager import EnvManager
from workbench.gateway.core import Gateway
from workbench.gateway.policy import ApprovalService
from workbench.gateway.server import create_gateway_app

pytestmark = pytest.mark.integration


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_gateway_read_ok_destructive_denied_and_audited(manager: EnvManager, tmp_path: Path) -> None:
    env = await manager.start("mini_e_commerce", session_id="gw1")
    audit = tmp_path / "audit.jsonl"
    gateway = Gateway(GatewaySettings(audit_path=audit), approvals=ApprovalService(secret=b"test"))
    await gateway.register_session("gw1", env.scenario, env.url)

    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(create_gateway_app(gateway), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {"X-Workbench-Session": "gw1"}
        async with streamablehttp_client(url, headers=headers) as (r, w, _), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert "mini_e_commerce__search_products" in names
            assert "mini_e_commerce__delete_user_payment_method" in names

            read = await s.call_tool("mini_e_commerce__list_user_payment_methods", {})
            assert not read.isError and "mastercard" in read.content[0].text

            denied = await s.call_tool(
                "mini_e_commerce__delete_user_payment_method", {"payment_method_id": 2}
            )
            assert denied.isError
            body = json.loads(denied.content[0].text)
            assert body["status"] == "denied" and body["decision"] == "approval_required"

            bad = await s.call_tool("mini_e_commerce__search_products", {"query": "x", "sort_by": "bogus"})
            err = json.loads(bad.content[0].text)["error"]
            assert err["code"] == "invalid_arguments" and err["details"]["allowed_values"] == [
                "price",
                "rating",
            ]

            empty = await s.call_tool("mini_e_commerce__search_products", {"query": "no-such-product"})
            assert not empty.isError and empty.structuredContent == {
                "status": "empty",
                "trace_id": empty.structuredContent["trace_id"],
            }

        token = gateway.issue_approval(
            "gw1", "mini_e_commerce__delete_user_payment_method", {"payment_method_id": 2}, "alice"
        )
        async with (
            streamablehttp_client(url, headers={**headers, "X-Approval-Token": token}) as (r, w, _),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
            ok = await s.call_tool("mini_e_commerce__delete_user_payment_method", {"payment_method_id": 2})
            assert not ok.isError
    finally:
        server.should_exit = True
        await task

    # the denial left an audit trace; the approved call recorded its approver
    rows = [json.loads(ln) for ln in audit.read_text().splitlines()]
    decisions = [(r["tool"].split("__")[1], r["decision"], r["approver"]) for r in rows]
    assert ("delete_user_payment_method", "approval_required", None) in decisions
    assert ("delete_user_payment_method", "allowed", "alice") in decisions
    assert manager.diff("gw1").tables["payment_methods"].removed == [2]
