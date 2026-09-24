from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from workbench.config import GatewaySettings
from workbench.gateway.audit import AuditLogger
from workbench.gateway.core import Gateway, UnknownSessionError
from workbench.gateway.policy import ApprovalService, PolicyConfig
from workbench.gateway.upstream import ToolSpec, UpstreamTimeoutError

TOOLS = [
    ToolSpec(
        "search_products", "Search products", {"type": "object", "properties": {"query": {"type": "string"}}}
    ),
    ToolSpec("add_item_to_cart", "Add item", {"type": "object", "required": ["product_id"]}),
    ToolSpec("delete_user_payment_method", "Delete pm", {"type": "object"}),
    ToolSpec("list_cart_items", "List cart", {"type": "object"}),
]


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, tuple[bool, str] | Exception] = {}

    async def list_tools(self, url: str, timeout_s: float) -> list[ToolSpec]:
        return list(TOOLS)

    async def call_tool(
        self, url: str, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> tuple[bool, str]:
        self.calls.append((name, arguments))
        r = self.responses.get(name, (False, '[{"id": 1}]'))
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def gw(tmp_path: Path) -> tuple[Gateway, FakeUpstream]:
    up = FakeUpstream()
    policy = PolicyConfig.load(Path("configs/tool_policy.yaml"))
    policy = PolicyConfig(policy.verbs, policy.unknown_default, policy.require_approval, {}, 2, 0.0001)
    g = Gateway(
        GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
        policy=policy,
        approvals=ApprovalService(secret=b"k"),
        upstream=up,
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )
    return g, up


async def test_prefixed_listing_and_allowlist(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, _ = gw
    await g.register_session("s1", "mini", "http://x/mcp", allowlist=["search_products", "add_item_to_cart"])
    assert {t.name for t in g.list_tools("s1")} == {"mini__search_products", "mini__add_item_to_cart"}
    table = {r["name"]: r for r in g.risk_table("s1")}
    assert table["mini__delete_user_payment_method"]["allowlisted"] is False
    assert table["mini__delete_user_payment_method"]["risk"] == "destructive"
    assert table["mini__add_item_to_cart"]["requires_approval"] is True


async def test_read_allowed_and_audited(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, up = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    out = await g.call_tool("s1", "mini__search_products", {"query": "bob@example.com"}, trace_id="tr1")
    assert out.status == "ok" and up.calls == [("search_products", {"query": "bob@example.com"})]
    row = g.audit.read("s1")[0]
    assert row["trace_id"] == "tr1" and row["decision"] == "allowed" and "[EMAIL]" in row["args_summary"]


async def test_destructive_denied_without_approval_then_allowed(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, up = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    args = {"payment_method_id": 2}
    denied = await g.call_tool("s1", "mini__delete_user_payment_method", args)
    assert denied.status == "denied" and denied.decision == "approval_required" and up.calls == []
    token = g.issue_approval("s1", "mini__delete_user_payment_method", args, approver="alice")
    ok = await g.call_tool("s1", "mini__delete_user_payment_method", args, approval_token=token)
    assert ok.status == "ok" and ok.approver == "alice"
    reused = await g.call_tool("s1", "mini__delete_user_payment_method", args, approval_token=token)
    assert reused.decision == "approval_invalid"
    rows = g.audit.read("s1")
    assert [r["decision"] for r in rows] == ["approval_required", "allowed", "approval_invalid"]
    assert rows[1]["approver"] == "alice"


async def test_unknown_and_cross_scenario_tools(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, _ = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    for name in ["other__search_products", "mini__nope", "noprefix"]:
        out = await g.call_tool("s1", name, {})
        assert out.status == "error" and out.error is not None and out.error["code"] == "unknown_tool"
        assert "available tools" in out.error["hints"][0]


async def test_rate_limit(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, _ = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    assert (await g.call_tool("s1", "mini__search_products", {})).status == "ok"
    assert (await g.call_tool("s1", "mini__search_products", {})).status == "ok"
    third = await g.call_tool("s1", "mini__search_products", {})
    assert third.decision == "rate_limited" and third.error is not None and third.error["retryable"]


async def test_empty_vs_error_vs_timeout(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, up = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    up.responses["search_products"] = (False, "[]")
    assert (await g.call_tool("s1", "mini__search_products", {})).status == "empty"
    up.responses["list_cart_items"] = (
        True,
        "Error calling list_cart_items. Status code: 500. Response: boom",
    )
    err = await g.call_tool("s1", "mini__list_cart_items", {})
    assert err.status == "error" and json.loads(err.text)["code"] == "upstream_server_error"
    up.responses["list_cart_items"] = UpstreamTimeoutError("slow")
    t = await g.call_tool("s1", "mini__list_cart_items", {})
    assert t.error is not None and t.error["code"] == "timeout"


async def test_unknown_session(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, _ = gw
    with pytest.raises(UnknownSessionError):
        await g.call_tool("nope", "mini__search_products", {})


async def test_empty_status_only_for_read_tools(gw: tuple[Gateway, FakeUpstream]) -> None:
    g, up = gw
    await g.register_session("s1", "mini", "http://x/mcp")
    up.responses["list_cart_items"] = (False, '{"cart_id": 1, "items": []}')
    up.responses["add_item_to_cart"] = (False, '{"cart_id": 1, "items": []}')
    assert (await g.call_tool("s1", "mini__list_cart_items", {})).status == "empty"
    token = g.issue_approval("s1", "mini__add_item_to_cart", {"product_id": 1}, approver="alice")
    out = await g.call_tool("s1", "mini__add_item_to_cart", {"product_id": 1}, approval_token=token)
    assert out.decision == "allowed" and out.status == "ok"
