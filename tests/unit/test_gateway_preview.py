"""Gateway side of approval previews (ADR-029, owner decision D29), with a fake preview backend.

The backend's change records come from real SQLite files (workbench.envs.changes), so their
format is the one the env-manager produces; the shadow environment itself is covered by
tests/integration/test_preview_real_awm.py.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_gateway_core import FakeUpstream
from workbench.config import ApprovalSettings, GatewaySettings
from workbench.envs.changes import changes, digest
from workbench.gateway.audit import AuditLogger
from workbench.gateway.core import Gateway
from workbench.gateway.policy import PREVIEW_UNAVAILABLE, ApprovalError, ApprovalService, PolicyConfig

ADD = "mini__add_item_to_cart"
DELETE = "mini__delete_user_payment_method"


def db_changes(tmp_path: Path, name: str, *statements: str) -> dict[str, Any]:
    base = tmp_path / "base.db"
    if not base.exists():
        with closing(sqlite3.connect(base)) as conn:
            conn.executescript(
                "CREATE TABLE cart_items (id INTEGER PRIMARY KEY, product_id INTEGER, quantity INTEGER,"
                " added_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
                "CREATE TABLE payment_methods (id INTEGER PRIMARY KEY, brand TEXT);"
                "INSERT INTO cart_items (id, product_id, quantity, added_at) VALUES (1, 7, 1, '2026-01-01');"
                "INSERT INTO payment_methods VALUES (1, 'Visa'), (2, 'MasterCard');"
            )
    after = tmp_path / f"{name}.db"
    shutil.copy2(base, after)
    with closing(sqlite3.connect(after)) as conn:
        for s in statements:
            conn.execute(s)
        conn.commit()
    return changes(base, after)


class FakePreviews:
    """Preview backend: canned preview result and the changes the real call is 'measured' to make."""

    def __init__(self, preview: dict[str, Any], actual: dict[str, Any]) -> None:
        self.preview_result = preview
        self.actual = actual
        self.calls: list[tuple[str, ...]] = []

    async def preview(
        self, session_id: str, tool: str, arguments: dict[str, Any], timeout_s: float, max_rows: int = 20
    ) -> dict[str, Any]:
        self.calls.append(("preview", session_id, tool))
        return dict(self.preview_result)

    async def checkpoint(self, session_id: str) -> str:
        self.calls.append(("checkpoint", session_id))
        return "cp1"

    async def changes_since(self, session_id: str, checkpoint: str) -> dict[str, Any]:
        self.calls.append(("changes_since", session_id, checkpoint))
        return self.actual


def ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"id": "p1", "status": "ok", "changes": result, "digest": digest(result), "timings_ms": {}}


@pytest.fixture
def make(tmp_path: Path) -> Any:
    async def build(previews: FakePreviews | None, **require: bool) -> tuple[Gateway, FakeUpstream]:
        up = FakeUpstream()
        settings = ApprovalSettings(require_preview={"write": False, "destructive": True, **require})
        g = Gateway(
            GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
            policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
            approvals=ApprovalService(secret=b"k"),
            upstream=up,
            audit=AuditLogger(tmp_path / "audit.jsonl"),
            approval=settings,
            previews=previews,
        )
        await g.register_session("s1", "mini", "http://x/mcp")
        return g, up

    return build


async def test_destructive_approval_needs_a_successful_preview(make: Any, tmp_path: Path) -> None:
    removed = db_changes(tmp_path, "rm", "DELETE FROM payment_methods WHERE id = 2")
    backend = FakePreviews(ok(removed), removed)
    g, up = await make(backend)
    args = {"payment_method_id": 2}
    with pytest.raises(ApprovalError, match="no preview was run"):
        g.issue_approval("s1", DELETE, args, "alice")

    record = await g.preview("s1", DELETE, args)
    assert record["status"] == "ok" and record["required"] and record["approvable"]
    assert record["summary"] == "payment_methods -1"
    assert record["changes"]["tables"]["payment_methods"]["removed"][0]["row"]["brand"] == "MasterCard"
    assert up.calls == []  # a preview never touches the session's own server

    token = g.issue_approval("s1", DELETE, args, "alice", preview=record)
    out = await g.call_tool("s1", DELETE, args, approval_token=token)
    assert out.status == "ok" and out.approver == "alice"
    assert out.preview == {"binding": record["digest"], "id": record["id"]}
    assert out.preview_check is not None and out.preview_check["result"] == "match"
    assert [c[0] for c in backend.calls] == ["preview", "checkpoint", "changes_since"]

    rows = g.audit.read("s1")
    assert [r["decision"] for r in rows] == ["preview", "allowed"]
    assert rows[0]["preview"]["status"] == "ok" and rows[0]["preview"]["required"] is True
    assert rows[1]["preview"]["binding"] == record["digest"] and rows[1]["preview_check"]["result"] == "match"


async def test_failed_preview_only_allows_rejection_when_required(make: Any) -> None:
    failed = {"id": "p2", "status": "failed", "error": "preview timed out after 30s (stage: start)"}
    g, _ = await make(FakePreviews(failed, {}))
    record = await g.preview("s1", DELETE, {"payment_method_id": 2})
    assert record["status"] == "failed" and record["approvable"] is False
    assert record["error"] == "preview timed out after 30s (stage: start)" == record["summary"]
    with pytest.raises(ApprovalError, match="the preview failed: preview timed out"):
        g.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice", preview=record)
    assert g.audit.read("s1")[0]["status"] == "failed"


async def test_write_without_preview_is_bound_to_preview_unavailable(make: Any, tmp_path: Path) -> None:
    added = db_changes(tmp_path, "add", "INSERT INTO cart_items (product_id, quantity) VALUES (11, 1)")
    failed = {"id": "p3", "status": "failed", "error": "shadow server exited with code 1", "stage": "start"}
    g, _ = await make(FakePreviews(failed, added))
    record = await g.preview("s1", ADD, {"product_id": 11})
    assert record["required"] is False and record["approvable"] is True  # 未预演, still approvable
    token = g.issue_approval("s1", ADD, {"product_id": 11}, "alice", preview=record)
    out = await g.call_tool("s1", ADD, {"product_id": 11}, approval_token=token)
    assert out.preview == {"binding": PREVIEW_UNAVAILABLE, "id": None}
    assert out.preview_check == {
        "result": "preview_unavailable",
        "reason": "approved without a successful preview",
        "actual": "cart_items +1",
    }
    assert g.audit.read("s1")[-1]["preview_check"]["result"] == "preview_unavailable"


async def test_a_different_real_change_is_audited_as_preview_mismatch(make: Any, tmp_path: Path) -> None:
    preview = db_changes(tmp_path, "p", "INSERT INTO cart_items (product_id, quantity) VALUES (11, 1)")
    actual = db_changes(
        tmp_path,
        "a",
        "INSERT INTO cart_items (product_id, quantity) VALUES (11, 1)",
        "UPDATE cart_items SET quantity = 3 WHERE id = 1",
    )
    g, _ = await make(FakePreviews(ok(preview), actual))
    record = await g.preview("s1", ADD, {"product_id": 11})
    token = g.issue_approval("s1", ADD, {"product_id": 11}, "alice", preview=record)
    out = await g.call_tool("s1", ADD, {"product_id": 11}, approval_token=token)
    check = out.preview_check
    assert check is not None and check["result"] == "preview_mismatch"
    assert check["differences"] == ['cart_items.changed: preview none, actual {1: ["quantity"]}']
    assert check["ignored_columns"] == {"cart_items": ["added_at"]}  # recorded, not compared
    assert (check["preview"], check["actual"]) == ("cart_items +1", "cart_items +1 ~1")
    assert g.audit.read("s1")[-1]["preview_check"]["result"] == "preview_mismatch"


async def test_arguments_that_do_not_match_the_token_are_refused(make: Any, tmp_path: Path) -> None:
    removed = db_changes(tmp_path, "rm", "DELETE FROM payment_methods WHERE id = 2")
    g, up = await make(FakePreviews(ok(removed), removed))
    record = await g.preview("s1", DELETE, {"payment_method_id": 2})
    token = g.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice", preview=record)
    other = await g.call_tool("s1", DELETE, {"payment_method_id": 1}, approval_token=token)
    assert other.status == "denied" and other.decision == "approval_invalid" and up.calls == []
    assert other.error is not None and "does not match" in other.error["message"]
    # the preview of one call cannot approve another: verification fails, so a destructive call is refused
    with pytest.raises(ApprovalError, match="could not be verified"):
        g.issue_approval("s1", DELETE, {"payment_method_id": 1}, "alice", preview=record)


async def test_tampered_or_foreign_preview_records_are_not_trusted(make: Any, tmp_path: Path) -> None:
    removed = db_changes(tmp_path, "rm", "DELETE FROM payment_methods WHERE id = 2")
    g, _ = await make(FakePreviews(ok(removed), removed))
    record = await g.preview("s1", DELETE, {"payment_method_id": 2})
    edited = {**record, "changes": {**record["changes"], "tables": {}}}  # what the approver "saw" differs
    with pytest.raises(ApprovalError, match="could not be verified"):
        g.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice", preview=edited)
    forged = {**record, "sig": "0" * 64}
    with pytest.raises(ApprovalError, match="could not be verified"):
        g.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice", preview=forged)
    # a gateway with another secret (e.g. after a restart without WORKBENCH_APPROVAL_SECRET)
    other, _ = await make(FakePreviews(ok(removed), removed))
    other.approvals = ApprovalService(secret=b"another")
    with pytest.raises(ApprovalError, match="could not be verified"):
        other.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice", preview=record)


async def test_without_an_env_service_previews_are_unavailable(make: Any) -> None:
    g, _ = await make(None)
    record = await g.preview("s1", ADD, {"product_id": 11})
    assert record["status"] == "unavailable" and "no env-manager" in record["error"]
    assert g.issue_approval("s1", ADD, {"product_id": 11}, "alice", preview=record)  # write: allowed
    with pytest.raises(ApprovalError):
        g.issue_approval("s1", DELETE, {"payment_method_id": 2}, "alice")
    strict, _ = await make(None, write=True)
    with pytest.raises(ApprovalError, match="approving a write call needs a successful preview"):
        strict.issue_approval("s1", ADD, {"product_id": 11}, "alice", preview=record)
