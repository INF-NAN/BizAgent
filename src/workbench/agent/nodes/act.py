from __future__ import annotations

import json
from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.guards import budget_guard, call_key, repeat_guard
from workbench.agent.nodes.common import llm_tools, terminate
from workbench.agent.nodes.intake import Node
from workbench.agent.state import AgentState, PendingCall
from workbench.llm.errors import LLMError


def make_act(deps: AgentDeps) -> Node:
    async def act(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        if term := budget_guard(state, deps.settings, deps.clock):
            deps.hub.emit(sid, "terminated", **term)
            return terminate(state, term)
        try:
            result = await deps.llm.chat(state["messages"], llm_tools(state["tools"]), purpose="act")
        except LLMError as exc:
            term = {"reason": "llm_error", "detail": str(exc)}
            deps.hub.emit(sid, "terminated", **term)
            return terminate(state, term)
        tokens = state.get("tokens_used", 0) + result.usage.total_tokens
        steps = state.get("steps", 0) + 1
        deps.hub.emit(sid, "llm", purpose="act", step=steps, tokens=result.usage.total_tokens)
        messages = list(state["messages"])
        if not result.tool_calls:
            messages.append({"role": "assistant", "content": result.content})
            deps.hub.emit(sid, "node", node="act", step=steps, answer=result.content[:500])
            return {
                "messages": messages,
                "draft_answer": result.content,
                "steps": steps,
                "tokens_used": tokens,
                "next": "verify",
            }
        tc = result.tool_calls[0]  # one tool per turn, like AWM's own agent (awm/core/agent.py:520-528)
        counts = dict(state.get("call_counts", {}))
        key = call_key(tc.name, tc.arguments)
        if term := repeat_guard(counts, key, deps.settings):
            deps.hub.emit(sid, "terminated", tool=tc.name, **term)
            return {**terminate(state, term), "steps": steps, "tokens_used": tokens}
        counts[key] = counts.get(key, 0) + 1
        messages.append(
            {
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                ],
            }
        )
        risk = next((t["risk"] for t in state["tools"] if t["name"] == tc.name), "unknown")
        # The approval policy decides per call (ADR-030): only require_human waits for a person;
        # auto_approve, deny and plain reads go straight to the gateway, which enforces the same answer.
        verdict = deps.gateway.approval_verdict(sid, tc.name, tc.arguments) if "__" in tc.name else None
        policy = verdict.as_dict() if verdict is not None else None
        needs_person = verdict is not None and verdict.decision == "require_human"
        pending: PendingCall = {
            "id": tc.id,
            "name": tc.name,
            "arguments": tc.arguments,
            "risk": risk,
            "policy": policy,
        }
        call = {"tool": tc.name, "arguments": tc.arguments, "risk": risk, "policy": policy}
        deps.hub.emit(sid, "node", node="act", step=steps, **call)
        if needs_person:
            # emitted here, not in `approve`: LangGraph re-runs an interrupted node on resume
            deps.hub.emit(sid, "approval_requested", **call)
        return {
            "messages": messages,
            "pending_call": pending,
            "call_counts": counts,
            "steps": steps,
            "tokens_used": tokens,
            "next": "preview" if needs_person else "observe",
        }

    return act
