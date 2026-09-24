from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.state import AgentState

Node = Callable[[AgentState], Awaitable[dict[str, Any]]]


def make_intake(deps: AgentDeps) -> Node:
    async def intake(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        # Tool list and descriptions are pulled from the gateway at runtime (Phase 5 rule).
        tools = [t.as_dict(deps.gateway.requires_approval(sid, t.name)) for t in deps.gateway.list_tools(sid)]
        memories = (
            [m.as_dict() for m in deps.memory.list(state.get("user_id", "default"))] if deps.memory else []
        )
        deps.hub.emit(sid, "node", node="intake", tools=len(tools), memories=len(memories))
        return {
            "tools": tools,
            "memories": memories,
            "started_at": deps.clock(),
            "steps": 0,
            "call_counts": {},
            "no_change": 0,
            "last_state_digest": "",
            "tokens_used": state.get("tokens_used", 0),
            "plan_attempts": 0,
            "verify_rounds": 0,
            "messages": [],
            "pending_call": None,
            "approval_token": None,
            "rejection": None,
            "termination": None,
            "draft_answer": "",
            "final_answer": "",
            "next": "plan",
        }

    return intake
