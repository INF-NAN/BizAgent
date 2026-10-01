"""AgentRunner: run / resume a thread and stream operational events.

Events streamed to callers are (a) TraceHub events emitted by nodes (tool calls, approvals,
memory writes, terminations) and (b) node-completion markers. When the graph pauses on an
approval interrupt the stream ends with ``{"type": "approval_required", ...}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langgraph.types import Command

from workbench.agent.deps import AgentDeps
from workbench.agent.graph import build_graph


class AgentRunner:
    def __init__(self, deps: AgentDeps, checkpointer: Any) -> None:
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)

    @staticmethod
    def config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}

    async def run(
        self, thread_id: str, session_id: str, request: str, user_id: str = "default"
    ) -> AsyncIterator[dict[str, Any]]:
        inputs = {"session_id": session_id, "user_id": user_id, "request": request}
        async for event in self._stream(thread_id, session_id, inputs):
            yield event

    async def resume(
        self, thread_id: str, session_id: str, decision: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._stream(thread_id, session_id, Command(resume=decision)):
            yield event

    async def pending_approval(self, thread_id: str) -> dict[str, Any] | None:
        snap = await self.graph.aget_state(self.config(thread_id))
        for task in snap.tasks:
            for intr in task.interrupts:
                value: dict[str, Any] = intr.value
                return value
        return None

    async def _stream(self, thread_id: str, session_id: str, payload: Any) -> AsyncIterator[dict[str, Any]]:
        queue = self.deps.hub.subscribe(session_id)
        try:
            async for chunk in self.graph.astream(payload, self.config(thread_id), stream_mode="updates"):
                while not queue.empty():
                    yield queue.get_nowait()
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        for intr in update:
                            yield {"type": "approval_required", "thread_id": thread_id, **intr.value}
                    else:
                        yield {"type": "node_done", "node": node}
            while not queue.empty():
                yield queue.get_nowait()
            snap = await self.graph.aget_state(self.config(thread_id))
            if not snap.next:
                yield {
                    "type": "done",
                    "final_answer": snap.values.get("final_answer", ""),
                    "termination": snap.values.get("termination"),
                }
        except asyncio.CancelledError:
            self.deps.hub.emit(session_id, "cancelled", thread_id=thread_id)
            raise
        finally:
            self.deps.hub.unsubscribe(session_id, queue)
