"""Phase 5 acceptance: one complete agent flow on a real AWM env with the mock LLM."""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tests.integration.conftest import MINI, free_base
from workbench.config import Settings
from workbench.runtime import Runtime

pytestmark = pytest.mark.integration


def settings_for(tmp_path: Path) -> Settings:
    base = free_base()
    return Settings(
        env={"dataset_dir": MINI, "runs_dir": tmp_path / "runs", "port_min": base, "port_max": base + 30},  # type: ignore[arg-type]
        gateway={"audit_path": tmp_path / "audit.jsonl"},  # type: ignore[arg-type]
        llm={
            "backend": "mock_replay",
            "mock_fixture": Path("tests/fixtures/trajectories/demo_query_write_approve.jsonl"),
        },  # type: ignore[arg-type]
        agent={"checkpoint_db": tmp_path / "ckpt.sqlite", "memory_db": tmp_path / "mem.sqlite"},  # type: ignore[arg-type]
    )


async def test_agent_query_write_approve_on_real_env(tmp_path: Path) -> None:
    rt = Runtime(settings_for(tmp_path), checkpointer=InMemorySaver())
    try:
        s = await rt.create_session("mini_e_commerce")
        assert rt.runner is not None
        first = [
            e
            async for e in rt.runner.run(
                s.thread_id, s.session_id, "Add the best headphones under $200 to my cart"
            )
        ]
        assert first[-1]["type"] == "approval_required"
        preview = first[-1]["preview"]  # ran in a shadow env on the real AWM server (ADR-029)
        assert preview["status"] == "ok" and preview["summary"] == "cart_items +1"
        assert (await rt.envs.diff(s.session_id))["changed"] is False  # the preview wrote nothing here
        searched = [e for e in first if e["type"] == "tool_call"]
        assert searched[0]["status"] == "ok" and "Headphones A" in searched[0]["text"]

        second = [
            e
            async for e in rt.runner.resume(
                s.thread_id, s.session_id, {"approved": True, "approver": "alice"}
            )
        ]
        assert second[-1]["type"] == "done" and second[-1]["termination"] is None
        diff = await rt.envs.diff(s.session_id)
        assert diff["tables"]["cart_items"]["added"] == [2]  # the approved write really happened
        audit = rt.gateway.audit.read(s.session_id)
        assert [r["decision"] for r in audit] == ["allowed", "preview", "allowed"]
        assert audit[2]["approver"] == "alice" and audit[2]["preview_check"]["result"] == "match"
        assert rt.trace_path(s.session_id) and rt.trace_path(s.session_id).exists()  # type: ignore[union-attr]
    finally:
        await rt.aclose()
