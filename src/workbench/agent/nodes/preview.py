from __future__ import annotations

from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.nodes.intake import Node
from workbench.agent.state import AgentState


def make_preview(deps: AgentDeps) -> Node:
    """Run the pending write/destructive call in a shadow environment before asking for approval.

    A node of its own, between act and approve: LangGraph re-runs an interrupted node from the
    start on resume (docs/RECON.md), so a preview inside `approve` would run twice. The record is
    kept in the checkpointed state and shown on the approval card (ADR-029).
    """

    async def preview(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        pc = state["pending_call"]
        assert pc is not None
        if pc["risk"] == "read":  # a read an approval rule sends to a person changes no rows (ADR-030)
            return {"pending_call": {**pc, "preview": None}, "next": "approve"}
        record = await deps.gateway.preview(sid, pc["name"], pc["arguments"])
        deps.hub.emit(
            sid,
            "preview",
            tool=pc["name"],
            status=record["status"],
            summary=record.get("summary"),
            required=record.get("required"),
            approvable=record.get("approvable"),
            timings_ms=record.get("timings_ms"),
        )
        return {"pending_call": {**pc, "preview": record}, "next": "approve"}

    return preview
