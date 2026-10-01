from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from workbench.agent.deps import AgentDeps
from workbench.agent.guards import budget_guard
from workbench.agent.nodes.common import (
    Plan,
    act_system_prompt,
    extract_json,
    memory_context,
    terminate,
    tools_block,
)
from workbench.agent.nodes.intake import Node
from workbench.agent.prompts import load_prompt
from workbench.agent.state import AgentState
from workbench.llm.errors import LLMError


def make_plan(deps: AgentDeps) -> Node:
    async def plan(state: AgentState) -> dict[str, Any]:
        sid = state["session_id"]
        if term := budget_guard(state, deps.settings, deps.clock):
            return terminate(state, term)
        prompt = load_prompt("plan")
        rejection = state.get("rejection")
        user = state["request"] + memory_context(state.get("memories", []))
        if rejection:
            user += f"\n\nA previous action was rejected by the human reviewer: {rejection}. Plan around it."
        msgs: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": f"{prompt.text}\n\nAvailable tools:\n{tools_block(state['tools'])}",
            },
            {"role": "user", "content": user},
        ]
        tokens = state.get("tokens_used", 0)
        attempts = state.get("plan_attempts", 0)
        parsed: Plan | None = None
        for _ in range(deps.settings.plan_retries + 1):
            attempts += 1
            try:
                result = await deps.llm.chat(msgs, purpose="plan")
            except LLMError as exc:
                return {
                    **terminate(state, {"reason": "llm_error", "detail": str(exc)}),
                    "tokens_used": tokens,
                }
            tokens += result.usage.total_tokens
            deps.hub.emit(
                sid, "llm", purpose="plan", tokens=result.usage.total_tokens, prompt_version=prompt.version
            )
            try:
                parsed = Plan.model_validate_json(extract_json(result.content))
                break
            except (ValidationError, ValueError) as exc:
                error = str(exc).splitlines()[0] if isinstance(exc, ValueError) else exc.errors()[0]["msg"]
                deps.hub.emit(sid, "plan_invalid", attempt=attempts, error=str(exc)[:500])
                msgs += [
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": f"That plan was invalid ({error}). Reply again with ONLY the JSON object.",
                    },
                ]
        if parsed is None:
            detail = f"no valid plan after {deps.settings.plan_retries + 1} attempts"
            return {
                **terminate(state, {"reason": "plan_invalid", "detail": detail}),
                "tokens_used": tokens,
                "plan_attempts": attempts,
            }
        steps = [s.model_dump() for s in parsed.steps]
        deps.hub.emit(sid, "node", node="plan", steps=steps)
        act = load_prompt("act")
        history = [m for m in state.get("messages", []) if m.get("role") != "system"]
        system = act_system_prompt(act.text, steps, state["tools"])
        messages = [{"role": "system", "content": system}, *(history or [{"role": "user", "content": user}])]
        if rejection and history:
            messages.append(
                {
                    "role": "user",
                    "content": f"Reviewer rejected the last action: {rejection}. Continue with the new plan.",
                }
            )
        return {
            "plan": steps,
            "plan_attempts": attempts,
            "messages": messages,
            "tokens_used": tokens,
            "rejection": None,
            "next": "act",
        }

    return plan
