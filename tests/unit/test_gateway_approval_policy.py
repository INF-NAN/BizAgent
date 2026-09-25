"""The approval policy enforced by the gateway (ADR-030, owner decision D32): auto_approve issues the
token on the policy's behalf without a preview, deny refuses even with a token, require_human needs
a person, destructive calls are never approved by the policy, and every audit line names the rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_gateway_core import FakeUpstream
from tests.unit.test_gateway_preview import FakePreviews, db_changes
from workbench.config import ApprovalSettings, GatewaySettings
from workbench.gateway.approval_policy import GUARD, ApprovalPolicy, ApprovalPolicyError, Verdict
from workbench.gateway.audit import AuditLogger
from workbench.gateway.core import Gateway
from workbench.gateway.policy import PREVIEW_UNAVAILABLE, ApprovalError, ApprovalService, PolicyConfig

ADD = "mini__add_item_to_cart"
DELETE = "mini__delete_user_payment_method"
SEARCH = "mini__search_products"
RULES: list[dict[str, Any]] = [
    {"id": "no-pm-delete", "tools": "delete_*payment_method*", "decision": "deny"},
    {
        "id": "careful-search",
        "tools": "search_products",
        "args": {"query": {"in": ["vip"]}},
        "decision": "require_human",
    },
    {
        "id": "small-add",
        "tools": "add_item_to_cart",
        "risk": ["write"],
        "args": {"quantity": {"lte": 2}},
        "decision": "auto_approve",
    },
]


async def gateway(
    tmp_path: Path,
    rules: list[dict[str, Any]],
    previews: FakePreviews | None = None,
    policy: PolicyConfig | None = None,
) -> tuple[Gateway, FakeUpstream]:
    up = FakeUpstream()
    g = Gateway(
        GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
        policy=policy or PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=up,
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        previews=previews,
        approval_policy=ApprovalPolicy.model_validate({"version": 1, "rules": rules}),
    )
    await g.register_session("s1", "mini", "http://x/mcp")
    return g, up


async def test_auto_approved_write_runs_without_a_preview_and_is_measured(tmp_path: Path) -> None:
    added = db_changes(tmp_path, "add", "INSERT INTO cart_items (product_id, quantity) VALUES (11, 1)")
    backend = FakePreviews({"status": "ok"}, added)
    g, up = await gateway(tmp_path, RULES, previews=backend)
    args = {"product_id": 11, "quantity": 1}
    assert g.approval_verdict("s1", ADD, args).decision == "auto_approve"  # the agent calls it directly

    out = await g.call_tool("s1", ADD, args)  # no token: the gateway issues one for the rule
    assert out.status == "ok" and out.decision == "allowed" and up.calls == [("add_item_to_cart", args)]
    assert out.approver == "policy:small-add"
    assert out.preview == {"binding": PREVIEW_UNAVAILABLE, "id": None}
    assert out.preview_check == {
        "result": "preview_unavailable",
        "reason": "auto-approved by approval policy rule small-add; no preview is run",
        "actual": "cart_items +1",  # the real change is still measured (D32)
    }
    assert [c[0] for c in backend.calls] == ["checkpoint", "changes_since"]  # no preview was run
    assert out.policy is not None and (out.policy["decision"], out.policy["rule"]) == (
        "auto_approve",
        "small-add",
    )
    row = g.audit.read("s1")[-1]
    assert (row["decision"], row["approver"], row["policy"]["rule"]) == (
        "allowed",
        "policy:small-add",
        "small-add",
    )
    assert row["preview_check"]["actual"] == "cart_items +1"

    # outside the rule (quantity 3) nothing matches: a write needs a person
    more = await g.call_tool("s1", ADD, {"product_id": 11, "quantity": 3})
    assert (more.status, more.decision) == ("denied", "approval_required") and len(up.calls) == 1
    assert more.error is not None and "no rule matched: write calls need a human" in more.error["message"]
    assert more.policy is not None and more.policy["rule"] is None


async def test_a_denied_call_is_refused_even_with_a_token(tmp_path: Path) -> None:
    g, up = await gateway(tmp_path, RULES)
    args = {"payment_method_id": 2}
    out = await g.call_tool("s1", DELETE, args)
    assert (out.status, out.decision) == ("denied", "denied_by_rule") and up.calls == []
    assert out.error is not None and out.error["code"] == "policy_denied_by_rule"
    assert "rule no-pm-delete" in out.error["message"]
    with pytest.raises(ApprovalError, match="the approval policy refuses this call"):
        g.issue_approval("s1", DELETE, args, "alice")
    # a token obtained some other way does not help: the rule applies before any token
    token = g.approvals.issue("s1", "delete_user_payment_method", args, "alice")
    again = await g.call_tool("s1", DELETE, args, approval_token=token)
    assert again.decision == "denied_by_rule" and up.calls == []
    assert [r["policy"]["rule"] for r in g.audit.read("s1")] == ["no-pm-delete", "no-pm-delete"]


async def test_a_require_human_rule_needs_a_person_even_for_a_read(tmp_path: Path) -> None:
    g, up = await gateway(tmp_path, RULES)
    assert g.approval_verdict("s1", SEARCH, {"query": "vip"}).decision == "require_human"
    held = await g.call_tool("s1", SEARCH, {"query": "vip"})
    assert (held.status, held.decision) == ("denied", "approval_required") and up.calls == []
    token = g.issue_approval("s1", SEARCH, {"query": "vip"}, "alice")  # a read needs no preview
    out = await g.call_tool("s1", SEARCH, {"query": "vip"}, approval_token=token)
    assert (out.status, out.approver) == ("ok", "alice")
    # other queries match no rule: a read needs nothing
    free = await g.call_tool("s1", SEARCH, {"query": "tv"})
    assert (free.status, free.approver) == ("ok", None)
    rules = [(r["decision"], r["policy"]["decision"], r["policy"]["rule"]) for r in g.audit.read("s1")]
    assert rules == [
        ("approval_required", "require_human", "careful-search"),
        ("allowed", "require_human", "careful-search"),
        ("allowed", "allow", None),
    ]


async def test_the_gateway_never_approves_a_destructive_call_on_the_policys_behalf(tmp_path: Path) -> None:
    """Defence in depth: even if evaluate() answered auto_approve for a destructive call (a bug or a
    replaced policy object), the gateway issues no token and asks for a person."""

    class Broken:
        def evaluate(self, **kw: Any) -> Verdict:
            return Verdict("auto_approve", "everything", "rule everything", kw["risk"], True)

    g, up = await gateway(tmp_path, [])
    g.approval_policy = Broken()  # type: ignore[assignment]
    out = await g.call_tool("s1", DELETE, {"payment_method_id": 2})
    assert (out.status, out.decision) == ("denied", "approval_required") and up.calls == []
    assert out.policy is not None and out.policy["guard"] == GUARD
    assert out.policy["decision"] == "require_human" and out.policy["rule"] == "everything"
    assert g.audit.read("s1")[-1]["policy"]["guard"] == GUARD
    # the same broken answer still approves a write: only destructive calls are guarded here
    ok = await g.call_tool("s1", ADD, {"product_id": 11})
    assert ok.status == "ok" and ok.approver == "policy:everything"


async def test_write_and_destructive_always_need_approval_without_a_rule(tmp_path: Path) -> None:
    """The tool policy's require_approval can add read, but cannot exempt write or destructive:
    only an auto_approve rule does (ADR-030)."""
    lax = PolicyConfig.load(Path("configs/tool_policy.yaml"))
    lax = PolicyConfig(lax.verbs, lax.unknown_default, frozenset({"destructive"}), {}, 10, 1)
    g, up = await gateway(tmp_path, [], policy=lax)
    assert g.requires_approval("s1", ADD) and not g.requires_approval("s1", SEARCH)
    out = await g.call_tool("s1", ADD, {"product_id": 11})
    assert out.decision == "approval_required" and up.calls == []
    strict = PolicyConfig(
        lax.verbs, lax.unknown_default, frozenset({"read", "write", "destructive"}), {}, 10, 1
    )
    g2, _ = await gateway(tmp_path, [], policy=strict)
    assert g2.requires_approval("s1", SEARCH)
    assert (await g2.call_tool("s1", SEARCH, {"query": "tv"})).decision == "approval_required"


def test_the_policy_file_is_validated_when_the_gateway_starts(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 1\nrules:\n  - {id: a, decision: deny, tool: x}\n", encoding="utf-8")
    settings = GatewaySettings(audit_path=tmp_path / "audit.jsonl")
    with pytest.raises(ApprovalPolicyError, match=r"rules\[0\] \(id a\)\.tool: unknown field"):
        Gateway(settings, approval=ApprovalSettings(policy_file=bad))
    shipped = Gateway(settings, approval=ApprovalSettings(policy_file=Path("configs/approval_policy.yaml")))
    assert shipped.approval_policy.rules[0].id == "no-payment-method-deletion"
    assert Gateway(settings).approval_policy.rules == []  # no file configured: no rules


async def test_admin_approvals_refuses_a_denied_call_without_running_a_preview(tmp_path: Path) -> None:
    import httpx

    from workbench.gateway.server import create_gateway_app

    backend = FakePreviews({"status": "ok"}, {})
    g, _ = await gateway(tmp_path, RULES, previews=backend)
    app = create_gateway_app(g)
    body = {"session_id": "s1", "tool": DELETE, "arguments": {"payment_method_id": 2}, "approver": "alice"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        r = await client.post("/admin/approvals", json=body)
    assert r.status_code == 409 and "nobody can approve it" in r.json()["error"]
    assert r.json()["policy"]["rule"] == "no-pm-delete" and backend.calls == []  # no preview was run
