from __future__ import annotations

import json
from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.guards import digest, no_change_guard
from workbench.agent.nodes.common import terminate
from workbench.agent.nodes.intake import Node
from workbench.agent.state import AgentState


def make_observe(deps: AgentDeps) -> Node:
    async def observe(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        pc = state["pending_call"]
        assert pc is not None
        outcome = await deps.gateway.call_tool(
            sid, pc["name"], pc["arguments"], approval_token=state.get("approval_token")
        )
        deps.hub.emit(sid, "tool_call", **outcome.as_dict(), arguments=pc["arguments"])
        check = outcome.preview_check or {}
        if check.get("result") == "preview_mismatch":  # flagged in the UI (ADR-029)
            deps.hub.emit(sid, "preview_mismatch", tool=pc["name"], differences=check.get("differences"))
        payload: dict[str, Any] = {"status": outcome.status}
        if outcome.error:
            payload["error"] = outcome.error
        else:
            payload["result"] = outcome.data if outcome.data is not None else outcome.text
        messages = [
            *state["messages"],
            {
                "role": "tool",
                "tool_call_id": pc["id"],
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]
        env_fp = deps.state_probe(sid) if deps.state_probe else ""
        fingerprint = digest([env_fp, outcome.status, outcome.text])
        no_change = state.get("no_change", 0) + 1 if fingerprint == state.get("last_state_digest") else 0
        update: dict[str, Any] = {
            "messages": messages,
            "pending_call": None,
            "approval_token": None,
            "last_state_digest": fingerprint,
            "no_change": no_change,
            "next": "act",
        }
        if term := no_change_guard(no_change, deps.settings):
            deps.hub.emit(sid, "terminated", **term)
            return {**update, **terminate(state, term)}
        return update

    return observe
