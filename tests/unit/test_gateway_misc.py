from pathlib import Path

from workbench.gateway.audit import AuditLogger, AuditRecord, redact, redact_text, summarize
from workbench.gateway.errors import is_empty_payload, normalize
from workbench.gateway.ratelimit import RateLimiter, TokenBucket


# --- rate limit ----------------------------------------------------------------
def test_token_bucket_refills() -> None:
    now = [0.0]
    b = TokenBucket(capacity=2, refill_per_s=1, clock=lambda: now[0])
    assert b.try_take() and b.try_take() and not b.try_take()
    assert b.retry_after_s() == 1.0
    now[0] += 1.0
    assert b.try_take() and not b.try_take()


def test_rate_limiter_is_per_session_and_tool() -> None:
    rl = RateLimiter(capacity=1, refill_per_s=0.0001, clock=lambda: 0.0)
    assert rl.check("s1", "t")[0]
    ok, retry = rl.check("s1", "t")
    assert not ok and retry > 0
    assert rl.check("s1", "other")[0]
    assert rl.check("s2", "t")[0]


# --- audit redaction -------------------------------------------------------------
def test_redacts_email_phone_card() -> None:
    text = "mail bob@example.com call +1 (555) 123-4567 card 4242 4242 4242 4242 on 2026-09-24"
    out = redact_text(text)
    assert "bob@example.com" not in out and "[EMAIL]" in out
    assert "555" not in out and "[PHONE]" in out
    assert "4242 4242" not in out and "[CARD ****4242]" in out
    assert "2026-09-24" in out  # dates are not phone numbers


def test_redact_nested_and_summary() -> None:
    data = {"user": {"email": "a@b.co", "phones": ["13812345678"]}, "qty": 2}
    red = redact(data)
    assert red == {"user": {"email": "[EMAIL]", "phones": ["[PHONE]"]}, "qty": 2}
    assert summarize("x" * 50, 10) == "xxxxxxx..."


def test_audit_jsonl(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "a" / "audit.jsonl")
    rec = AuditRecord("t1", "s1", "sc__tool", "read", "allowed", None, "{}", "[]", "empty", 1.0)
    log.write(rec)
    log.write(AuditRecord("t2", "s2", "sc__tool", "read", "allowed", None, "{}", "[]", "ok", 1.0))
    rows = log.read("s1")
    assert len(rows) == 1 and rows[0]["trace_id"] == "t1" and rows[0]["ts"] > 0


# --- error normalization --------------------------------------------------------
SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "sort_by": {"enum": ["price", "rating"], "type": "string"}},
    "required": ["query"],
}


def test_empty_is_not_error() -> None:
    for text in ["[]", "{}", "", "null", "  [ ] "]:
        r = normalize("t", False, text)
        assert r.status == "empty" and r.error is None, text
    assert normalize("t", False, '[{"id": 1}]').status == "ok"


def test_wrapped_empty_lists_are_empty() -> None:
    # shapes returned by the official e_commerce_33 environment (2026-09-24)
    for text in ['{"products": [], "total": 0}', '{"cart_id": 1, "items": []}', '{"items": []}']:
        assert is_empty_payload(text), text
    for text in [
        '{"success": false}',
        '{"products": [{"id": 1}], "total": 1}',
        '{"product": {"id": 0}, "aggregates": null, "active_offers": []}',
        '{"total": 0}',
    ]:
        assert not is_empty_payload(text), text


def test_type_violation_gets_expected_type() -> None:
    r = normalize("get_product_by_id", True, "Input validation error: 'abc' is not of type 'integer'")
    assert r.error is not None and r.error.code == "invalid_arguments"
    assert r.error.details["expected_type"] == "'integer'"


def test_enum_violation_gets_allowed_values() -> None:
    r = normalize(
        "search_products", True, "Input validation error: 'bogus' is not one of ['price', 'rating']", SCHEMA
    )
    assert r.status == "error" and r.error is not None
    assert r.error.code == "invalid_arguments"
    assert r.error.details["allowed_values"] == ["price", "rating"]
    assert any("one of" in h for h in r.error.hints)


def test_missing_field_hint() -> None:
    r = normalize("add_item_to_cart", True, "Input validation error: 'product_id' is a required property")
    assert r.error is not None and r.error.details["missing_fields"] == ["product_id"]


def test_http_500_and_404() -> None:
    r = normalize(
        "get_product_by_id",
        True,
        "Error calling get_product_by_id. Status code: 500. Response: Internal Server Error",
    )
    assert r.error is not None and r.error.code == "upstream_server_error"
    r = normalize("x", True, "Error calling x. Status code: 404. Response: {}")
    assert r.error is not None and r.error.code == "not_found"


def test_http_422_fastapi_detail() -> None:
    body = (
        '{"detail": [{"type": "missing", "loc": ["body", "quantity"], "msg": "Field required"},'
        ' {"type": "literal_error", "loc": ["query", "sort_by"], "msg": "bad",'
        ' "ctx": {"expected": "\'price\' or \'rating\'"}}]}'
    )
    r = normalize("t", True, f"Error calling t. Status code: 422. Response: {body}")
    assert r.error is not None and r.error.code == "invalid_arguments"
    assert r.error.details["missing_fields"] == ["quantity"]
    assert any("sort_by" in h for h in r.error.hints)


def test_unknown_upstream_error() -> None:
    r = normalize("t", True, "something odd")
    assert r.error is not None and r.error.code == "upstream_error"
