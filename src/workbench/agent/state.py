"""Agent graph state. Everything here must be JSON-serializable (it is checkpointed)."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

TerminationReason = Literal[
    "max_steps",
    "repeated_call",
    "no_state_change",
    "token_budget",
    "wall_clock",
    "plan_invalid",
    "llm_error",
]


class Termination(TypedDict):
    reason: TerminationReason
    detail: str


class PendingCall(TypedDict):
    id: str
    name: str
    arguments: dict[str, Any]
    risk: str
    # the gateway's signed preview record, for calls that need approval (ADR-029)
    preview: NotRequired[dict[str, Any] | None]


class AgentState(TypedDict, total=False):
    session_id: str
    user_id: str
    request: str
    tools: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    plan: list[dict[str, Any]]
    plan_attempts: int
    messages: list[dict[str, Any]]
    steps: int
    call_counts: dict[str, int]
    last_state_digest: str
    no_change: int
    pending_call: PendingCall | None
    approval_token: str | None
    rejection: str | None
    tokens_used: int
    started_at: float
    draft_answer: str
    verify_rounds: int
    final_answer: str
    termination: Termination | None
    next: str
