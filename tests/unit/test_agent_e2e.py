"""Phase 5 acceptance scenarios on CPU with the mock LLM (mechanisms only — no quality claims)."""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from tests.unit.agent_harness import collect, make_runner, types


async def test_normal_completion(tmp_path: Path) -> None:
    runner, up, deps = await make_runner(tmp_path, "e2e_normal.jsonl")
    events = await collect(runner.run("t1", "s1", "Which headphones are rated best?"))
    done = events[-1]
    assert done["type"] == "done" and done["termination"] is None
    assert "Headphones B" in done["final_answer"]
    assert up.calls == [("search_products", {"query": "Headphones", "sort_by": "rating"})]
    assert "tool_call" in types(events) and "approval_required" not in types(events)
    assert deps.llm.usage.total_tokens > 0


async def test_write_needs_approval_and_is_approved(tmp_path: Path) -> None:
    runner, up, deps = await make_runner(tmp_path, "demo_query_write_approve.jsonl")
    first = await collect(
        runner.run("t2", "s1", "Add the best wireless noise cancelling headphones under $200 to my cart")
    )
    assert first[-1]["type"] == "approval_required"
    assert first[-1]["tool"] == "mini_e_commerce__add_item_to_cart" and first[-1]["risk"] == "write"
    assert [c[0] for c in up.calls] == ["search_products"]  # write not executed yet
    pending = await runner.pending_approval("t2")
    assert pending is not None and pending["arguments"] == {"product_id": 1, "quantity": 1}

    second = await collect(runner.resume("t2", "s1", {"approved": True, "approver": "alice"}))
    assert [c[0] for c in up.calls] == ["search_products", "add_item_to_cart"]
    assert second[-1]["type"] == "done" and "added" in second[-1]["final_answer"].lower()
    audit = deps.gateway.audit.read("s1")  # type: ignore[attr-defined]
    assert audit[-1]["approver"] == "alice" and audit[-1]["decision"] == "allowed"


async def test_rejection_triggers_replan(tmp_path: Path) -> None:
    runner, up, deps = await make_runner(tmp_path, "e2e_reject_replan.jsonl")
    first = await collect(runner.run("t3", "s1", "Delete my mastercard"))
    assert first[-1]["type"] == "approval_required"
    second = await collect(
        runner.resume("t3", "s1", {"approved": False, "approver": "bob", "reason": "keep it"})
    )
    assert "approval_rejected" in types(second)
    assert [c[0] for c in up.calls] == ["list_user_payment_methods"]  # delete never executed
    assert second[-1]["type"] == "done" and "Nothing was deleted" in second[-1]["final_answer"]
    plan_prompt = deps.llm.backend.requests[2][1]["content"]  # type: ignore[attr-defined]
    assert "keep it" in plan_prompt  # rejection reason fed back into planning


async def test_repeated_call_terminates(tmp_path: Path) -> None:
    runner, up, _ = await make_runner(tmp_path, "e2e_repeat.jsonl", repeat_call_threshold=3)
    events = await collect(runner.run("t4", "s1", "search"))
    assert events[-1]["termination"]["reason"] == "repeated_call"
    assert len(up.calls) == 2  # the 3rd identical call is stopped before execution


async def test_plan_validation_retry(tmp_path: Path) -> None:
    runner, _, _ = await make_runner(tmp_path, "e2e_plan_retry.jsonl")
    events = await collect(runner.run("t5", "s1", "what do you sell?"))
    assert types(events).count("plan_invalid") == 2
    assert events[-1]["type"] == "done" and events[-1]["termination"] is None


async def test_plan_invalid_after_max_retries(tmp_path: Path) -> None:
    runner, _, deps = await make_runner(tmp_path, "e2e_plan_invalid.jsonl", plan_retries=2)
    events = await collect(runner.run("t6", "s1", "x"))
    assert events[-1]["termination"]["reason"] == "plan_invalid"
    assert deps.llm.backend.remaining == 0  # type: ignore[attr-defined]


async def test_no_state_change_terminates(tmp_path: Path) -> None:
    runner, up, _ = await make_runner(tmp_path, "e2e_no_change.jsonl", no_change_threshold=2)
    events = await collect(runner.run("t7", "s1", "find zzz"))
    assert events[-1]["termination"]["reason"] == "no_state_change"
    assert len(up.calls) == 3


async def test_token_budget_and_max_steps(tmp_path: Path) -> None:
    runner, _, _ = await make_runner(tmp_path, "e2e_normal.jsonl", token_budget=500)
    events = await collect(runner.run("t8", "s1", "x"))
    assert events[-1]["termination"]["reason"] == "token_budget"
    runner, _, _ = await make_runner(
        tmp_path / "b", "e2e_repeat.jsonl", max_steps=1, repeat_call_threshold=10
    )
    events = await collect(runner.run("t9", "s1", "x"))
    assert events[-1]["termination"]["reason"] == "max_steps"


async def test_wall_clock_guard(tmp_path: Path) -> None:
    runner, _, deps = await make_runner(tmp_path, "e2e_normal.jsonl", wall_clock_s=10)
    now = [1000.0]

    def clock() -> float:
        now[0] += 6  # every read of the clock advances 6s
        return now[0]

    deps.clock = clock
    events = await collect(runner.run("t10", "s1", "x"))
    assert events[-1]["termination"]["reason"] == "wall_clock"


async def test_resume_from_checkpoint_after_restart(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        runner, _, _ = await make_runner(tmp_path, "demo_query_write_approve.jsonl", checkpointer=saver)
        first = await collect(runner.run("t11", "s1", "buy headphones"))
        assert first[-1]["type"] == "approval_required"
    # "process restart": fresh gateway/LLM/runner, same checkpoint file, LLM continues at turn 4
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver2:
        runner2, up2, _ = await make_runner(
            tmp_path / "p2", "demo_query_write_approve.jsonl", checkpointer=saver2, start_at=3
        )
        assert (await runner2.pending_approval("t11")) is not None
        second = await collect(runner2.resume("t11", "s1", {"approved": True, "approver": "carol"}))
    assert [c[0] for c in up2.calls] == ["add_item_to_cart"]
    assert second[-1]["type"] == "done"


async def test_memory_gate(tmp_path: Path) -> None:
    runner, _, deps = await make_runner(tmp_path, "demo_query_write_approve.jsonl")
    await collect(runner.run("t12", "s1", "buy headphones"))
    events = await collect(runner.resume("t12", "s1", {"approved": True, "approver": "alice"}))
    saved = [e for e in events if e["type"] == "memory_saved"]
    rejected = [e for e in events if e["type"] == "memory_rejected"]
    assert [e["key"] for e in saved] == ["headphone_budget"]
    assert [e["source"] for e in rejected] == ["inferred"]
    assert deps.memory is not None
    assert [m.key for m in deps.memory.list("default")] == ["headphone_budget"]


async def test_trace_persisted(tmp_path: Path) -> None:
    runner, _, deps = await make_runner(tmp_path, "e2e_normal.jsonl")
    await collect(runner.run("t13", "s1", "x"))
    trace = deps.hub.events("s1")
    assert (tmp_path / "runs" / "s1" / "trace.jsonl").exists()
    assert {"node", "llm", "tool_call", "final"} <= {e["type"] for e in trace}
