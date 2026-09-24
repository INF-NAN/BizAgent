"""Helpers shared by nodes."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from workbench.agent.state import AgentState, Termination

JSON_OBJECT = re.compile(r"\{.*\}", re.S)
THINK = re.compile(r"<think>.*?</think>", re.S)


class PlanStep(BaseModel):
    description: str = Field(min_length=1)
    tool: str | None = None
    changes_data: bool = False


class Plan(BaseModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=10)


class MemoryProposal(BaseModel):
    key: str
    value: str
    source: str


class Verification(BaseModel):
    complete: bool
    missing: list[str] = Field(default_factory=list)
    memories: list[MemoryProposal] = Field(default_factory=list)


def extract_json(text: str) -> str:
    """Take the outermost {...} from a model reply (tolerates think blocks and prose)."""
    cleaned = THINK.sub("", text).strip()
    m = JSON_OBJECT.search(cleaned)
    if not m:
        raise ValueError("no JSON object found in the reply")
    return m.group(0)


def tools_block(tools: list[dict[str, Any]]) -> str:
    """Render the tool list fetched at runtime from the gateway (never hand-written)."""
    lines = []
    for t in tools:
        approval = ", needs approval" if t.get("requires_approval") else ""
        lines.append(f"- {t['name']} [risk: {t['risk']}{approval}]: {t.get('description', '')}")
        lines.append(f"  arguments schema: {json.dumps(t.get('input_schema', {}), ensure_ascii=False)}")
    return "\n".join(lines) if lines else "(no tools available)"


def llm_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("input_schema", {}),
        }
        for t in tools
    ]


def terminate(state: AgentState, term: Termination) -> dict[str, Any]:
    return {"termination": term, "next": "respond"}


def memory_context(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    facts = "; ".join(f"{m['key']}: {m['value']} ({m['source']})" for m in memories)
    return f"\n\nKnown facts about this user: {facts}"
