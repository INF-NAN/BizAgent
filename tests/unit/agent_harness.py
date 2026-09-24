"""Shared harness for agent tests: real Gateway + fake upstream (mini semantics) + mock LLM."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.memory import MemoryService
from workbench.agent.runner import AgentRunner
from workbench.config import AgentSettings, GatewaySettings, LLMSettings
from workbench.gateway.core import Gateway
from workbench.gateway.policy import ApprovalService, PolicyConfig
from workbench.gateway.upstream import ToolSpec
from workbench.llm.backends.mock_replay import MockReplayBackend
from workbench.llm.client import LLMClient
from workbench.obs.tracing import TraceHub

FIX = Path("tests/fixtures/trajectories")
SCENARIO = "mini_e_commerce"
# (product id, title, offer id, price, average rating); response shapes follow the official
# e_commerce_33 tools (docs/verification/2026-09-24-fixture-reconciliation.md)
PRODUCTS = [
    (1, "Wireless Noise Cancelling Headphones A", 11, 189.0, 4.7),
    (2, "Wireless Noise Cancelling Headphones B", 12, 249.0, 4.8),
]


class MiniUpstream:
    """Fake AWM server with the mini scenario's tool names and simple semantics."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, url: str, timeout_s: float) -> list[ToolSpec]:
        names = [
            "search_products",
            "get_product_by_id",
            "list_cart_items",
            "add_item_to_cart",
            "remove_cart_item",
            "list_user_payment_methods",
            "delete_user_payment_method",
        ]
        return [ToolSpec(n, n.replace("_", " "), {"type": "object", "properties": {}}) for n in names]

    async def call_tool(
        self, url: str, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> tuple[bool, str]:
        self.calls.append((name, arguments))
        if name == "search_products":
            q = str(arguments.get("query", "")).lower()
            hits = [p for p in PRODUCTS if q and q in p[1].lower()]
            if "max_price" in arguments:
                hits = [p for p in hits if p[3] <= arguments["max_price"]]
            items = [
                {
                    "product": {"id": pid, "title": title},
                    "aggregates": {"product_id": pid, "average_rating": rating},
                    "lowest_active_offer": {"id": oid, "product_id": pid, "price": price, "currency": "USD"},
                }
                for pid, title, oid, price, rating in sorted(hits, key=lambda p: -p[4])
            ]
            return False, json.dumps({"products": items, "total": len(items)})
        if name == "add_item_to_cart":
            return False, json.dumps({"cart_item": {"id": 2, "cart_id": 1, **arguments}})
        if name == "list_user_payment_methods":
            methods = [{"id": 1, "card_brand": "Visa"}, {"id": 2, "card_brand": "MasterCard"}]
            return False, json.dumps({"payment_methods": methods})
        if name == "delete_user_payment_method":
            return False, json.dumps({"success": True})
        return False, json.dumps({"items": []})


async def make_runner(
    tmp_path: Path,
    fixture: str,
    *,
    checkpointer: Any = None,
    start_at: int = 0,
    upstream: MiniUpstream | None = None,
    **agent_overrides: Any,
) -> tuple[AgentRunner, MiniUpstream, AgentDeps]:
    from langgraph.checkpoint.memory import InMemorySaver

    up = upstream or MiniUpstream()
    gateway = Gateway(
        GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
        policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=up,
    )
    await gateway.register_session("s1", SCENARIO, "http://fake/mcp")
    llm = LLMClient(MockReplayBackend(FIX / fixture, start_at=start_at), LLMSettings())
    deps = AgentDeps(
        llm=llm,
        gateway=gateway,
        settings=AgentSettings(**agent_overrides),
        hub=TraceHub(tmp_path / "runs"),
        memory=MemoryService(tmp_path / "memory.sqlite", ttl_s=3600),
    )
    return AgentRunner(deps, checkpointer or InMemorySaver()), up, deps


async def collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e async for e in stream]


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]
