"""Dependencies injected into graph nodes (kept out of the checkpointed state)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from workbench.agent.memory import MemoryService
from workbench.config import AgentSettings
from workbench.gateway.core import CallOutcome, GatewayTool
from workbench.llm.client import LLMClient
from workbench.obs.tracing import TraceHub


class GatewayLike(Protocol):
    def list_tools(self, session_id: str) -> list[GatewayTool]: ...
    def requires_approval(self, session_id: str, prefixed: str) -> bool: ...
    def issue_approval(
        self, session_id: str, prefixed: str, arguments: dict[str, Any], approver: str
    ) -> str: ...
    async def call_tool(
        self,
        session_id: str,
        prefixed: str,
        arguments: dict[str, Any] | None = None,
        *,
        approval_token: str | None = None,
        trace_id: str | None = None,
    ) -> CallOutcome: ...


@dataclass
class AgentDeps:
    llm: LLMClient
    gateway: GatewayLike
    settings: AgentSettings
    hub: TraceHub
    memory: MemoryService | None = None
    # Optional environment-state fingerprint (e.g. a hash of the session DB) used by the
    # "no state change" guard; falls back to fingerprinting tool results.
    state_probe: Callable[[str], str] | None = None
    clock: Callable[[], float] = field(default=time.time)
