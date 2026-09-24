from pathlib import Path

import pytest

from workbench.gateway.policy import (
    ApprovalError,
    ApprovalService,
    PolicyConfig,
    PolicyEngine,
    classify,
)

CFG = PolicyConfig.load(Path("configs/tool_policy.yaml"))


@pytest.mark.parametrize(
    ("tool", "desc", "level"),
    [
        ("search_products", "Search products by keyword", "read"),
        ("get_product_by_id", "", "read"),
        ("list_cart_items", "", "read"),
        ("add_item_to_cart", "", "write"),
        ("update_user_address", "", "write"),
        ("set_default_payment_method", "", "write"),
        ("create_user_wishlist", "", "write"),
        ("delete_user_payment_method", "", "destructive"),
        ("remove_cart_item", "", "destructive"),
        ("cancel_order", "", "destructive"),
        ("refund_payment", "", "destructive"),
        ("transfer_funds", "", "destructive"),
        # mixed verbs: the highest risk wins
        ("get_or_create_active_cart", "", "write"),
        ("list_and_delete_items", "", "destructive"),
        # verb only in the description
        ("orders_endpoint", "Delete an order", "destructive"),
        # unknown verb -> conservative default
        ("submit_order_from_active_cart", "", "write"),
    ],
)
def test_verb_heuristic(tool: str, desc: str, level: str) -> None:
    assert classify(tool, desc, CFG).level == level


def test_override_beats_heuristic() -> None:
    cfg = PolicyConfig(
        verbs=CFG.verbs,
        overrides={"mini__search_products": "destructive", "list_cart_items": "write"},
    )
    c = classify("search_products", "", cfg, scenario="mini")
    assert (c.level, c.source) == ("destructive", "override")
    assert classify("search_products", "", cfg, scenario="other").level == "read"
    assert classify("list_cart_items", "", cfg).level == "write"


def test_invalid_level_rejected(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text("verbs: {}\nunknown_default: dangerous\n", encoding="utf-8")
    with pytest.raises(ValueError):
        PolicyConfig.load(p)


# --- approvals ---------------------------------------------------------------
def test_token_is_single_use_and_bound() -> None:
    svc = ApprovalService(secret=b"k")
    tok = svc.issue("s1", "delete_x", {"id": 1}, approver="alice")
    with pytest.raises(ApprovalError, match="match"):
        svc.consume(tok, "s1", "delete_x", {"id": 2})
    with pytest.raises(ApprovalError, match="match"):
        svc.consume(tok, "s2", "delete_x", {"id": 1})
    assert svc.consume(tok, "s1", "delete_x", {"id": 1}) == "alice"
    with pytest.raises(ApprovalError, match="already used"):
        svc.consume(tok, "s1", "delete_x", {"id": 1})


def test_token_forged_and_expired() -> None:
    now = [100.0]
    svc = ApprovalService(secret=b"k", ttl_s=10, clock=lambda: now[0])
    other = ApprovalService(secret=b"other")
    forged = other.issue("s1", "t", {}, "mallory")
    with pytest.raises(ApprovalError, match="signature"):
        svc.consume(forged, "s1", "t", {})
    tok = svc.issue("s1", "t", {}, "bob")
    now[0] += 11
    with pytest.raises(ApprovalError, match="expired"):
        svc.consume(tok, "s1", "t", {})
    with pytest.raises(ApprovalError, match="malformed"):
        svc.consume("garbage", "s1", "t", {})


# --- policy matrix: 3 levels x {no token, valid token} + allowlist -----------
@pytest.mark.parametrize(
    ("risk", "with_token", "allowed", "code"),
    [
        ("read", False, True, "allowed"),
        ("read", True, True, "allowed"),
        ("write", False, False, "approval_required"),
        ("write", True, True, "allowed"),
        ("destructive", False, False, "approval_required"),
        ("destructive", True, True, "allowed"),
    ],
)
def test_policy_matrix(risk: str, with_token: bool, allowed: bool, code: str) -> None:
    svc = ApprovalService(secret=b"k")
    engine = PolicyEngine(CFG, svc)
    token = svc.issue("s", "tool", {"a": 1}, "carol") if with_token else None
    d = engine.decide(
        session_id="s",
        tool="tool",
        risk=risk,  # type: ignore[arg-type]
        allowlist=frozenset({"tool"}),
        arguments={"a": 1},
        approval_token=token,
    )
    assert (d.allowed, d.code) == (allowed, code)
    if allowed and risk != "read":
        assert d.approver == "carol"


def test_deny_first_allowlist_even_for_read() -> None:
    engine = PolicyEngine(CFG, ApprovalService(secret=b"k"))
    d = engine.decide(
        session_id="s", tool="t", risk="read", allowlist=frozenset(), arguments={}, approval_token=None
    )
    assert (d.allowed, d.code) == (False, "not_allowlisted")


def test_approval_levels_configurable() -> None:
    cfg = PolicyConfig(verbs=CFG.verbs, require_approval=frozenset({"destructive"}))
    engine = PolicyEngine(cfg, ApprovalService(secret=b"k"))
    d = engine.decide(
        session_id="s", tool="t", risk="write", allowlist=frozenset({"t"}), arguments={}, approval_token=None
    )
    assert d.allowed


def test_invalid_token_denied() -> None:
    engine = PolicyEngine(CFG, ApprovalService(secret=b"k"))
    d = engine.decide(
        session_id="s", tool="t", risk="write", allowlist=frozenset({"t"}), arguments={}, approval_token="x.y"
    )
    assert (d.allowed, d.code) == (False, "approval_invalid")


@pytest.mark.parametrize(
    ("tool", "method", "level", "source"),
    [
        # read verbs in write routes are raised to the method floor (ADR-015)
        ("purge_my_list_by_maturity_level", "DELETE", "destructive", "http_method"),
        ("attach_contact_list_to_collector", "POST", "write", "http_method"),
        ("record_answer_view", "POST", "write", "http_method"),
        ("get_trip_price_quote", "post", "write", "http_method"),  # case-insensitive
        # unknown verb on a DELETE route: default write -> destructive
        ("clear_cart", "DELETE", "destructive", "http_method"),
        # already at or above the floor: heuristic result kept
        ("update_user_address", "PUT", "write", "heuristic"),
        ("add_item_to_cart", "PATCH", "write", "heuristic"),
        ("delete_user_payment_method", "DELETE", "destructive", "heuristic"),
        ("list_and_delete_items", "POST", "destructive", "heuristic"),
        # GET, other methods and tools missing from the catalog add no floor
        ("list_cart_items", "GET", "read", "heuristic"),
        ("list_cart_items", "HEAD", "read", "heuristic"),
        ("list_cart_items", None, "read", "heuristic"),
        ("submit_order_from_active_cart", None, "write", "default"),
    ],
)
def test_http_method_floor(tool: str, method: str | None, level: str, source: str) -> None:
    c = classify(tool, "", CFG, http_method=method)
    assert (c.level, c.source) == (level, source)


def test_floor_reason_keeps_the_heuristic_verdict() -> None:
    c = classify("purge_my_list_by_maturity_level", "", CFG, http_method="DELETE")
    assert c.reason == "DELETE route => at least destructive (was: verb 'list' => read)"


def test_description_verb_cannot_lower_a_write_route() -> None:
    # "Get or create ..." made this POST route a read before the floor existed
    c = classify("ensure_direct_dm_with_user", "Get or create 1:1 DM by tag", CFG, http_method="POST")
    assert (c.level, c.source) == ("write", "http_method")


def test_override_is_applied_as_written_even_below_the_floor() -> None:
    cfg = PolicyConfig(verbs=CFG.verbs, overrides={"get_trip_price_quote": "read"})
    c = classify("get_trip_price_quote", "", cfg, http_method="POST")
    assert (c.level, c.source) == ("read", "override")
