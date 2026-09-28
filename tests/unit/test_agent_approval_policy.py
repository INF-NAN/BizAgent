"""The agent routes each call by the approval policy's answer (ADR-030, owner decision D32).

Mock LLM (the hand-written demo script) and a fake upstream; mechanisms only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.agent_harness import collect, make_runner, types

DEMO = "demo_query_write_approve.jsonl"
REQUEST = "Add the best wireless noise cancelling headphones under $200 to my cart"
ADD = "mini_e_commerce__add_item_to_cart"
SEARCH = "mini_e_commerce__search_products"


def tool_calls(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["tool"]: e for e in events if e["type"] == "tool_call"}


async def test_an_auto_approved_write_needs_no_person_and_no_preview(tmp_path: Path) -> None:
    rules = [
        {
            "id": "small-add",
            "tools": "add_item_to_cart",
            "risk": ["write"],
            "args": {"quantity": {"lte": 2}},
            "decision": "auto_approve",
        }
    ]
    runner, up, deps = await make_runner(tmp_path, DEMO, approval_rules=rules)
    events = await collect(runner.run("t1", "s1", REQUEST))
    assert events[-1]["type"] == "done" and "added" in events[-1]["final_answer"].lower()
    assert not {"approval_required", "approval_requested", "preview"} & set(types(events))
    assert [c[0] for c in up.calls] == ["search_products", "add_item_to_cart"]
    write = tool_calls(events)[ADD]
    assert (write["status"], write["approver"]) == ("ok", "policy:small-add")
    assert write["policy"]["rule"] == "small-add" and write["policy"]["decision"] == "auto_approve"
    assert write["preview"] == {"binding": "preview_unavailable", "id": None}
    reason = "auto-approved by approval policy rule small-add; no preview is run"
    assert write["preview_check"]["reason"] == reason
    audit = deps.gateway.audit.read("s1")  # type: ignore[attr-defined]
    assert [(r["tool"], r["approver"], r["policy"]["rule"]) for r in audit] == [
        (SEARCH, None, None),
        (ADD, "policy:small-add", "small-add"),
    ]


async def test_a_denied_write_is_never_run_or_offered_for_approval(tmp_path: Path) -> None:
    rules = [{"id": "no-cart", "tools": "add_item_to_cart", "decision": "deny"}]
    runner, up, deps = await make_runner(tmp_path, DEMO, approval_rules=rules)
    events = await collect(runner.run("t2", "s1", REQUEST))
    assert "approval_required" not in types(events) and events[-1]["type"] == "done"
    assert [c[0] for c in up.calls] == ["search_products"]
    denied = tool_calls(events)[ADD]
    assert (denied["status"], denied["decision"], denied["policy"]["rule"]) == (
        "denied",
        "denied_by_rule",
        "no-cart",
    )
    audit = deps.gateway.audit.read("s1")  # type: ignore[attr-defined]
    assert (audit[-1]["decision"], audit[-1]["policy"]["rule"]) == ("denied_by_rule", "no-cart")


async def test_a_require_human_rule_holds_a_read_for_a_person_without_a_preview(tmp_path: Path) -> None:
    rules = [{"id": "careful-search", "tools": "search_products", "decision": "require_human"}]
    runner, up, _ = await make_runner(tmp_path, DEMO, approval_rules=rules)
    first = await collect(runner.run("t3", "s1", REQUEST))
    held = first[-1]
    assert held["type"] == "approval_required" and held["tool"] == SEARCH and held["risk"] == "read"
    assert held["policy"]["rule"] == "careful-search" and held["preview"] is None  # a read is not previewed
    assert "preview" not in types(first) and up.calls == []

    second = await collect(runner.resume("t3", "s1", {"approved": True, "approver": "alice"}))
    assert tool_calls(second)[SEARCH]["approver"] == "alice"
    # the write matches no rule: the default sends it to a person, after a preview
    write = second[-1]
    assert write["type"] == "approval_required" and write["tool"] == ADD
    assert write["policy"] == {
        "decision": "require_human",
        "rule": None,
        "reason": "no rule matched: write calls need a human by default",
        "guard": None,
    }
    assert write["preview"] is not None and "preview" in types(second)
    third = await collect(runner.resume("t3", "s1", {"approved": True, "approver": "alice"}))
    assert third[-1]["type"] == "done" and [c[0] for c in up.calls] == ["search_products", "add_item_to_cart"]
