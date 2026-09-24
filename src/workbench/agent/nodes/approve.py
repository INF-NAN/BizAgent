from __future__ import annotations

import json
from typing import Any

from langgraph.types import interrupt

from workbench.agent.deps import AgentDeps
from workbench.agent.nodes.intake import Node
from workbench.agent.state import AgentState


def make_approve(deps: AgentDeps) -> Node:
    async def approve(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        pc = state["pending_call"]
        assert pc is not None
        # Pauses the graph; the API resumes it with Command(resume={"approved": ..., ...}).
        decision: dict[str, Any] = interrupt(
            {
                "type": "approval_required",
                "session_id": sid,
                "tool": pc["name"],
                "arguments": pc["arguments"],
                "risk": pc["risk"],
            }
        )
        approver = str(decision.get("approver") or "unknown")
        if decision.get("approved"):
            token = deps.gateway.issue_approval(sid, pc["name"], pc["arguments"], approver=approver)
            deps.hub.emit(sid, "approval_granted", tool=pc["name"], approver=approver)
            return {"approval_token": token, "next": "observe"}
        reason = str(decision.get("reason") or "no reason given")
        deps.hub.emit(sid, "approval_rejected", tool=pc["name"], approver=approver, reason=reason)
        messages = [
            *state["messages"],
            {
                "role": "tool",
                "tool_call_id": pc["id"],
                "content": json.dumps({"status": "rejected", "reason": reason}),
            },
        ]
        return {
            "messages": messages,
            "pending_call": None,
            "rejection": f"{pc['name']} rejected by {approver}: {reason}",
            "next": "plan",
        }

    return approve
