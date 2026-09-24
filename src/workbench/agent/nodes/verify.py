from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from workbench.agent.deps import AgentDeps
from workbench.agent.guards import budget_guard
from workbench.agent.memory import MemoryRejectedError
from workbench.agent.nodes.common import Verification, extract_json, terminate
from workbench.agent.nodes.intake import Node
from workbench.agent.prompts import load_prompt
from workbench.agent.state import AgentState
from workbench.llm.errors import LLMError


def make_verify(deps: AgentDeps) -> Node:
    async def verify(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        if term := budget_guard(state, deps.settings, deps.clock):
            return terminate(state, term)
        prompt = load_prompt("verify")
        tool_results = [m["content"] for m in state["messages"] if m.get("role") == "tool"][-8:]
        context = {
            "request": state["request"],
            "plan": state.get("plan", []),
            "tool_results": tool_results,
            "draft_answer": state.get("draft_answer", ""),
        }
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        tokens = state.get("tokens_used", 0)
        parsed: Verification | None = None
        for _ in range(deps.settings.plan_retries + 1):
            try:
                result = await deps.llm.chat(msgs, purpose="verify")
            except LLMError as exc:
                return {
                    **terminate(state, {"reason": "llm_error", "detail": str(exc)}),
                    "tokens_used": tokens,
                }
            tokens += result.usage.total_tokens
            try:
                parsed = Verification.model_validate_json(extract_json(result.content))
                break
            except (ValidationError, ValueError) as exc:
                deps.hub.emit(sid, "verify_invalid", error=str(exc)[:300])
                msgs += [
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": "Invalid verification JSON. Reply again with ONLY the JSON object.",
                    },
                ]
        if parsed is None:
            deps.hub.emit(sid, "node", node="verify", complete=None)
            return {"tokens_used": tokens, "next": "respond"}
        user_id = state.get("user_id", "default")
        for mem in parsed.memories:
            if deps.memory is None:
                break
            try:
                deps.memory.put(user_id, mem.key, mem.value, mem.source, session_id=sid)
                deps.hub.emit(sid, "memory_saved", key=mem.key, source=mem.source)
            except MemoryRejectedError as exc:
                deps.hub.emit(sid, "memory_rejected", key=mem.key, source=mem.source, reason=str(exc))
        deps.hub.emit(sid, "node", node="verify", complete=parsed.complete, missing=parsed.missing)
        rounds = state.get("verify_rounds", 0)
        if parsed.complete or rounds >= 1:
            return {"tokens_used": tokens, "verify_rounds": rounds + 1, "next": "respond"}
        note = {"role": "user", "content": f"Verification: still missing {parsed.missing}. Continue."}
        return {
            "messages": [*state["messages"], note],
            "tokens_used": tokens,
            "verify_rounds": rounds + 1,
            "next": "act",
        }

    return verify
