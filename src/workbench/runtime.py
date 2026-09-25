"""Composition root: builds env service, gateway, LLM client, agent runner, memory and traces
from Settings. Shared by the CLI (`workbench agent run`) and the HTTP API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench.agent.deps import AgentDeps
from workbench.agent.memory import MemoryService
from workbench.agent.runner import AgentRunner
from workbench.config import Settings
from workbench.envs.manager import EnvManager
from workbench.envs.service import EnvInfo, EnvService, LocalEnvService, RemoteEnvService
from workbench.envs.snapshot import fingerprint
from workbench.gateway.core import CallOutcome, Gateway
from workbench.llm.client import LLMClient, build_llm_client
from workbench.obs.tracing import TraceHub


@dataclass
class SessionInfo:
    session_id: str
    scenario: str
    env: EnvInfo
    thread_id: str
    tools: list[dict[str, Any]] = field(default_factory=list)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        env_service: EnvService | None = None,
        gateway: Gateway | None = None,
        llm: LLMClient | None = None,
        checkpointer: Any = None,
        on_call: Any = None,
    ) -> None:
        self.settings = settings
        if env_service is None:
            if settings.env.manager_url:
                env_service = RemoteEnvService(settings.env.manager_url)
            else:
                env_service = LocalEnvService(EnvManager(settings.env))
        self.envs = env_service
        self.hub = TraceHub(settings.env.runs_dir)
        self.gateway = gateway or Gateway(settings.gateway, on_call=on_call, approval=settings.approval)
        if self.gateway.previews is None:
            self.gateway.attach_previews(env_service)  # approval previews run in the env-manager (ADR-029)
        self.llm = llm or build_llm_client(settings.llm)
        self.memory = MemoryService(settings.agent.memory_db, settings.agent.memory_ttl_s)
        self.sessions: dict[str, SessionInfo] = {}
        self.deps = AgentDeps(
            llm=self.llm,
            gateway=self.gateway,
            settings=settings.agent,
            hub=self.hub,
            memory=self.memory,
            state_probe=self._probe,
        )
        self._checkpointer = checkpointer
        self.runner = AgentRunner(self.deps, checkpointer) if checkpointer is not None else None

    def attach_checkpointer(self, checkpointer: Any) -> None:
        self._checkpointer = checkpointer
        self.runner = AgentRunner(self.deps, checkpointer)

    def _probe(self, session_id: str) -> str:
        db = self.settings.env.runs_dir / session_id / "work.db"
        return fingerprint(db) if db.exists() else ""

    async def create_session(
        self, scenario: str, allowlist: list[str] | None = None, session_id: str | None = None
    ) -> SessionInfo:
        sid = session_id or uuid.uuid4().hex[:12]
        env = await self.envs.start(scenario, sid)
        try:
            await self.gateway.register_session(
                sid, env.scenario, env.url, allowlist, tool_methods=env.tool_methods
            )
        except BaseException:
            await self.envs.stop(sid)
            raise
        info = SessionInfo(
            session_id=sid,
            scenario=env.scenario,
            env=env,
            thread_id=f"{sid}-thread",
            tools=[
                t.as_dict(self.gateway.requires_approval(sid, t.name)) for t in self.gateway.list_tools(sid)
            ],
        )
        self.sessions[sid] = info
        self.hub.emit(sid, "session_created", scenario=env.scenario, tools=len(info.tools), url=env.url)
        return info

    async def close_session(self, session_id: str) -> None:
        self.gateway.unregister_session(session_id)
        self.sessions.pop(session_id, None)
        await self.envs.stop(session_id)

    async def call_tool(self, session_id: str, name: str, args: dict[str, Any]) -> CallOutcome:
        await self.envs.touch(session_id)
        return await self.gateway.call_tool(session_id, name, args)

    async def aclose(self) -> None:
        for sid in list(self.sessions):
            await self.close_session(sid)
        await self.envs.close()
        await self.llm.aclose()

    def trace_path(self, session_id: str) -> Path | None:
        return self.hub.path(session_id)
