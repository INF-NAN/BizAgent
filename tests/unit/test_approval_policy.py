"""The approval policy on its own (ADR-030): rule order, argument conditions, the destructive guard,
defaults without a match, and schema validation. The gateway side is in test_gateway_approval_policy.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from workbench.gateway.approval_policy import ApprovalPolicy, ApprovalPolicyError, Verdict


def policy(*rules: dict[str, Any]) -> ApprovalPolicy:
    return ApprovalPolicy.model_validate({"version": 1, "rules": list(rules)})


def decide(p: ApprovalPolicy, tool: str, risk: str, scenario: str = "mini", **args: Any) -> Verdict:
    return p.evaluate(scenario=scenario, tool=tool, risk=risk, arguments=args, needs_approval=risk != "read")


def test_rules_are_tried_in_order_and_the_first_match_decides() -> None:
    deny = {"id": "no-bulk", "tools": "add_item_to_cart", "args": {"quantity": {"gt": 5}}, "decision": "deny"}
    auto = {"id": "cart", "tools": ["add_*"], "risk": ["write"], "decision": "auto_approve"}
    first_deny = decide(policy(deny, auto), "add_item_to_cart", "write", quantity=9)
    assert (first_deny.decision, first_deny.rule, first_deny.needs_token) == ("deny", "no-bulk", False)
    assert [(c.rule, c.matched) for c in first_deny.checks] == [("no-bulk", True)]  # later rules not tried
    first_auto = decide(policy(auto, deny), "add_item_to_cart", "write", quantity=9)
    assert (first_auto.decision, first_auto.rule, first_auto.needs_token) == ("auto_approve", "cart", True)
    small = decide(policy(deny, auto), "add_item_to_cart", "write", quantity=1)
    assert small.rule == "cart" and [c.matched for c in small.checks] == [False, True]
    assert small.checks[0].reason == "quantity=1 fails gt 5"


def test_tool_scenario_and_risk_filters() -> None:
    p = policy(
        {"id": "official", "scenarios": ["e_commerce_*"], "tools": "add_item_to_cart", "decision": "deny"},
        {"id": "writes", "risk": ["write"], "decision": "require_human"},
    )
    assert decide(p, "add_item_to_cart", "write", scenario="e_commerce_33").rule == "official"
    # globs are anchored: mini_e_commerce is not e_commerce_*
    other = decide(p, "add_item_to_cart", "write", scenario="mini_e_commerce")
    assert other.rule == "writes" and "does not match ['e_commerce_*']" in other.checks[0].reason
    assert decide(p, "search_products", "read").decision == "allow"  # risk read is not in [write]


@pytest.mark.parametrize(
    ("condition", "value", "matched"),
    [
        ({"lt": 3}, 2, True),
        ({"lt": 3}, 3, False),
        ({"lte": 3}, 3, True),
        ({"gt": 3}, 3, False),
        ({"gte": 3}, 3.0, True),
        ({"eq": 2}, 2.0, True),
        ({"ne": 2}, 2, False),
        ({"gte": 1, "lte": 5}, 5, True),  # every operator must hold
        ({"gte": 1, "lte": 5}, 6, False),
        ({"in": ["standard", "express"]}, "express", True),
        ({"in": ["standard", "express"]}, "overnight", False),
        ({"not_in": [13, 14]}, 12, True),
        ({"not_in": [13, 14]}, 14, False),
        ({"in": [1, 2]}, 2.0, True),  # numbers compare as numbers
        ({"in": [1, 2]}, True, None),  # a boolean is not the number 1
        ({"in": [True]}, 1, None),
        ({"lte": 2}, "2", None),  # strings are not converted
        ({"lte": 2}, None, None),
        ({"in": ["a"]}, ["a"], None),
    ],
)
def test_numeric_and_enum_conditions(condition: dict[str, Any], value: Any, matched: bool | None) -> None:
    deny = policy({"id": "d", "tools": "t", "args": {"x": condition}, "decision": "deny"})
    auto = policy({"id": "a", "tools": "t", "args": {"x": condition}, "decision": "auto_approve"})
    denied, approved = decide(deny, "t", "write", x=value), decide(auto, "t", "write", x=value)
    if matched is None:  # cannot be compared: deny still applies, auto_approve does not
        assert denied.decision == "deny" and "treated as a match" in denied.checks[0].reason
        assert approved.decision == "require_human" and "treated as no match" in approved.checks[0].reason
    else:
        assert (denied.decision == "deny") is matched
        assert (approved.decision == "auto_approve") is matched


def test_a_missing_argument_never_matches() -> None:
    p = policy(
        {"id": "d", "tools": "t", "args": {"amount": {"gt": 100}}, "decision": "deny"},
        {"id": "a", "tools": "t", "args": {"amount": {"lte": 100}}, "decision": "auto_approve"},
    )
    v = decide(p, "t", "write", other=1)
    assert v.decision == "require_human" and v.rule is None
    assert [c.reason for c in v.checks] == ["argument amount is missing"] * 2


def test_destructive_calls_are_never_auto_approved() -> None:
    everything = {"id": "everything", "decision": "auto_approve"}  # no risk filter: matches destructive too
    v = decide(policy(everything), "remove_cart_item", "destructive", cart_item_id=1)
    assert (v.decision, v.rule, v.needs_token) == ("require_human", None, True)
    assert v.guard == "rule everything skipped: destructive calls are never auto-approved (ADR-030)"
    assert v.checks[0].matched is False and v.checks[0].reason.endswith("never auto-approved (ADR-030)")
    # the skipped rule does not hide a later, stricter one
    later = decide(
        policy(everything, {"id": "no", "tools": "remove_*", "decision": "deny"}),
        "remove_cart_item",
        "destructive",
    )
    assert (later.decision, later.rule) == ("deny", "no") and later.guard is not None
    # the same rule still auto-approves writes
    assert decide(policy(everything), "add_item_to_cart", "write").decision == "auto_approve"
    # and a rule that says so explicitly does not load
    with pytest.raises(ValueError, match="cannot list 'destructive' in risk"):
        policy({"id": "x", "risk": ["write", "destructive"], "decision": "auto_approve"})


def test_defaults_without_a_matching_rule() -> None:
    empty = ApprovalPolicy.empty()
    for risk in ("write", "destructive"):
        v = decide(empty, "t", risk)
        assert (v.decision, v.rule, v.needs_token) == ("require_human", None, True)
        assert v.reason == f"no rule matched: {risk} calls need a human by default"
    read = decide(empty, "t", "read")
    assert (read.decision, read.needs_token) == ("allow", False)
    # reads can need approval when the tool policy says so (require_approval: [read, ...])
    strict = empty.evaluate(scenario="s", tool="t", risk="read", arguments={}, needs_approval=True)
    assert strict.decision == "require_human"
    # auto_approve on a read call that needs no approval simply allows it (no token)
    auto = decide(policy({"id": "reads", "risk": ["read"], "decision": "auto_approve"}), "t", "read")
    assert (auto.decision, auto.rule, auto.needs_token) == ("auto_approve", "reads", False)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "approval_policy.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_schema_errors_name_every_problem(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
        version: 1
        colour: blue
        rules:
          - id: first
            tool: add_item_to_cart
            decision: deny
          - id: second
            args: {quantity: {lte: "2", between: 1}, note: {}}
            decision: maybe
          - id: third
            risk: [admin]
            tools: []
            decision: require_human
        """,
    )
    with pytest.raises(ApprovalPolicyError) as err:
        ApprovalPolicy.load(path)
    lines = str(err.value).splitlines()
    assert lines[0] == f"{path}: invalid approval policy:"
    assert lines[1:] == [  # pydantic lists unknown fields after the declared ones of the same model
        "  - rules[0] (id first).tool: unknown field",
        "  - rules[1] (id second).args.quantity.lte: must be a number (a string or a boolean is not)",
        "  - rules[1] (id second).args.quantity.between: unknown field",
        "  - rules[1] (id second).args.note: give at least one operator: "
        "lt, lte, gt, gte, eq, ne, in, not_in",
        "  - rules[1] (id second).decision: Input should be 'auto_approve', 'require_human' or 'deny'",
        "  - rules[2] (id third).tools: must be a non-empty glob or a non-empty list of globs",
        "  - rules[2] (id third).risk[0]: Input should be 'read', 'write' or 'destructive'",
        "  - colour: unknown field",
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("rules: []", "version: Field required"),
        ("version: 2\nrules: []", "version: Input should be 1"),
        ("version: 1\nrules:\n  - {id: a, decision: deny}\n  - {id: a, decision: deny}", "repeated: ['a']"),
        ("version: 1\nrules:\n  - {decision: deny}", "rules[0].id: Field required"),
        (
            "version: 1\nrules:\n  - {id: 'no spaces', decision: deny}",
            "rules[0] (id no spaces).id: String should match",
        ),
        (
            "version: 1\nrules:\n  - {id: a, args: {x: {in: []}}, decision: deny}",
            "rules[0] (id a).args.x.in: List should have at least 1 item",
        ),
        (
            "version: 1\nrules:\n  - {id: a, risk: [], decision: deny}",
            "rules[0] (id a).risk: List should have at least 1 item",
        ),
        ("- just\n- a list", "(file): Input should be a valid dictionary"),
        ("version: 1\nrules: [", "cannot read the approval policy"),
    ],
)
def test_schema_errors(tmp_path: Path, text: str, problem: str) -> None:
    with pytest.raises(ApprovalPolicyError) as err:
        ApprovalPolicy.load(write(tmp_path, text))
    assert problem in str(err.value)


def test_the_shipped_policy_loads_and_keeps_the_demo_call_for_a_person() -> None:
    p = ApprovalPolicy.load(Path("configs/approval_policy.yaml"))
    assert [r.id for r in p.rules] == [
        "no-payment-method-deletion",
        "bulk-cart-add-needs-human",
        "small-cart-add-official",
    ]
    demo = decide(p, "add_item_to_cart", "write", scenario="mini_e_commerce", product_offer_id=11, quantity=1)
    assert (demo.decision, demo.rule) == ("require_human", None)  # the demo still shows the approval card
    official = decide(
        p, "add_item_to_cart", "write", scenario="e_commerce_33", product_offer_id=1, quantity=2
    )
    assert (official.decision, official.rule) == ("auto_approve", "small-cart-add-official")
    bulk = decide(p, "add_item_to_cart", "write", scenario="e_commerce_33", product_offer_id=1, quantity=9)
    assert (bulk.decision, bulk.rule) == ("require_human", "bulk-cart-add-needs-human")
    pm = decide(
        p, "delete_user_payment_method", "destructive", scenario="mini_e_commerce", payment_method_id=2
    )
    assert (pm.decision, pm.rule) == ("deny", "no-payment-method-deletion")
