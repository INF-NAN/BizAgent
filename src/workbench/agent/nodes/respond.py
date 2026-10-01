from __future__ import annotations

from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.nodes.intake import Node
from workbench.agent.state import AgentState


def make_respond(deps: AgentDeps) -> Node:
    async def respond(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        term = state.get("termination")
        answer = state.get("draft_answer", "")
        if term:
            answer = (answer + "\n\n" if answer else "") + f"[stopped: {term['reason']} — {term['detail']}]"
        deps.hub.emit(
            sid,
            "final",
            answer=answer,
            termination=term,
            steps=state.get("steps", 0),
            tokens_used=state.get("tokens_used", 0),
        )
        return {"final_answer": answer, "next": "end"}

    return respond
