from pathlib import Path

import pytest

from workbench.agent.guards import budget_guard, call_key, no_change_guard, repeat_guard
from workbench.agent.memory import MemoryRejectedError, MemoryService
from workbench.agent.nodes.common import Plan, act_system_prompt, extract_json, tools_block, tools_risk_table
from workbench.agent.prompts import load_prompt
from workbench.config import AgentSettings


def test_prompts_versioned() -> None:
    for name in ("plan", "act", "verify"):
        p = load_prompt(name)
        assert p.version >= 1 and p.text


def test_prompt_header_required(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("no header\nbody", encoding="utf-8")
    with pytest.raises(ValueError):
        load_prompt("plan", tmp_path)


def test_guards() -> None:
    s = AgentSettings(
        max_steps=3, token_budget=100, wall_clock_s=10, repeat_call_threshold=2, no_change_threshold=2
    )
    assert budget_guard({"steps": 3}, s) is not None
    assert budget_guard({"tokens_used": 100}, s)["reason"] == "token_budget"  # type: ignore[index]
    assert budget_guard({"started_at": 0.0}, s, now=lambda: 11.0)["reason"] == "wall_clock"  # type: ignore[index]
    assert budget_guard({"steps": 1, "tokens_used": 1, "started_at": 5.0}, s, now=lambda: 6.0) is None
    k = call_key("t", {"a": 1})
    assert k == call_key("t", {"a": 1}) != call_key("t", {"a": 2})
    assert repeat_guard({}, k, s) is None and repeat_guard({k: 1}, k, s) is not None
    assert no_change_guard(1, s) is None and no_change_guard(2, s) is not None


def test_plan_schema_and_json_extraction() -> None:
    raw = '<think>hmm</think>Sure: {"steps": [{"description": "x"}]} done'
    assert Plan.model_validate_json(extract_json(raw)).steps[0].description == "x"
    with pytest.raises(ValueError):
        extract_json("nothing")


def test_tools_block_uses_runtime_tools() -> None:
    block = tools_block(
        [
            {
                "name": "sc__t",
                "risk": "write",
                "requires_approval": True,
                "description": "d",
                "input_schema": {},
            }
        ]
    )
    assert "sc__t" in block and "needs approval" in block


TOOLS = [
    {
        "name": "sc__search",
        "risk": "read",
        "requires_approval": False,
        "description": "Search the catalogue by keyword",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "sc__add",
        "risk": "write",
        "requires_approval": True,
        "description": "Add an item",
        "input_schema": {"type": "object", "required": ["offer_id"]},
    },
]


def test_act_system_prompt_lists_names_and_risk_only() -> None:
    # ADR-018: full definitions travel once, as the native tools parameter
    steps = [{"description": "find it", "tool": "sc__search", "changes_data": False}]
    system = act_system_prompt("ACT PROMPT", steps, TOOLS)
    assert system.startswith("ACT PROMPT") and '"description": "find it"' in system
    assert "- sc__search [risk: read]" in system and "- sc__add [risk: write, needs approval]" in system
    assert "Search the catalogue" not in system and "properties" not in system and "offer_id" not in system
    assert tools_risk_table([]) == "(no tools available)"


def test_act_prompt_version_bumped() -> None:
    p = load_prompt("act")
    assert p.version == 2 and "tools" in p.text


def test_defaults_follow_the_measurements() -> None:
    # ADR-018: measured on official e_commerce_33 (39 tools) in Phase 12 / 12.5
    from workbench.config import AgentSettings, LLMSettings, Settings

    assert AgentSettings().token_budget == 240_000 and LLMSettings().max_tokens == 8192
    s = Settings()  # configs/app.yaml must agree with the code defaults
    assert (s.agent.token_budget, s.llm.max_tokens) == (240_000, 8192)


def test_memory_gate_ttl_view_delete(tmp_path: Path) -> None:
    m = MemoryService(tmp_path / "m.sqlite", ttl_s=3600)
    m.put("u", "budget", "under $200", "user_stated")
    m.put("u", "order", "#42 shipped", "tool_result")
    with pytest.raises(MemoryRejectedError):
        m.put("u", "persona", "likes audio", "inferred")
    with pytest.raises(MemoryRejectedError):
        m.put("u", "", "x", "user_stated")
    assert {i.key for i in m.list("u")} == {"budget", "order"}
    assert m.delete("u", "budget") and not m.delete("u", "budget")
    assert [i.key for i in m.list("u")] == ["order"]
    assert m.list("other") == []


def test_memory_ttl_expiry(tmp_path: Path) -> None:
    m = MemoryService(tmp_path / "m.sqlite", ttl_s=0.001)  # ~0.06 ms TTL
    m.put("u", "k", "v", "user_stated")
    import time

    # LangGraph's SqliteStore compares expires_at (microseconds) with CURRENT_TIMESTAMP
    # (whole seconds) as strings (langgraph/store/sqlite/base.py:1139): expiry lands next second.
    time.sleep(1.2)
    assert m.list("u") == []
