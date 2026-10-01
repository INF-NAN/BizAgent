"""Termination guards. Each guard returns a Termination (with its own reason) or None."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from workbench.agent.state import AgentState, Termination
from workbench.config import AgentSettings


def call_key(name: str, arguments: dict[str, Any]) -> str:
    blob = json.dumps({"n": name, "a": arguments}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def budget_guard(
    state: AgentState, s: AgentSettings, now: Callable[[], float] = time.time
) -> Termination | None:
    """Checked before every LLM step: steps, tokens, wall clock."""
    if state.get("steps", 0) >= s.max_steps:
        return {"reason": "max_steps", "detail": f"reached {s.max_steps} steps"}
    if state.get("tokens_used", 0) >= s.token_budget:
        return {
            "reason": "token_budget",
            "detail": f"used {state.get('tokens_used', 0)} >= {s.token_budget} tokens",
        }
    elapsed = now() - state.get("started_at", now())
    if elapsed >= s.wall_clock_s:
        return {"reason": "wall_clock", "detail": f"{elapsed:.1f}s >= {s.wall_clock_s}s"}
    return None


def repeat_guard(counts: dict[str, int], key: str, s: AgentSettings) -> Termination | None:
    """Same tool + same arguments requested `repeat_call_threshold` times -> stop."""
    if counts.get(key, 0) + 1 >= s.repeat_call_threshold:
        return {
            "reason": "repeated_call",
            "detail": f"identical call repeated {s.repeat_call_threshold} times",
        }
    return None


def no_change_guard(no_change: int, s: AgentSettings) -> Termination | None:
    if no_change >= s.no_change_threshold:
        return {"reason": "no_state_change", "detail": f"{no_change} consecutive steps without state change"}
    return None
