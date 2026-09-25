"""FastAPI application: sessions, streamed agent turns, approvals, memory, traces, metrics, UI."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from workbench.api.schemas import ApprovalIn, CreateSession, MessageIn, SessionOut
from workbench.api.sse import to_sse
from workbench.config import Settings
from workbench.envs.catalog import build_catalog
from workbench.envs.manager import EnvError, EnvNotFoundError, EnvQueueTimeoutError, install_cleanup
from workbench.envs.service import LocalEnvService
from workbench.gateway.core import CallOutcome
from workbench.gateway.server import create_gateway_app
from workbench.obs.metrics import Metrics
from workbench.runtime import Runtime, SessionInfo


class ApiState:
    def __init__(self, settings: Settings, runtime: Runtime, metrics: Metrics) -> None:
        self.settings = settings
        self.runtime = runtime
        self.metrics = metrics
        self.busy: set[str] = set()
        self.approval_asked_at: dict[str, float] = {}


def create_app(settings: Settings, runtime: Runtime | None = None) -> FastAPI:
    metrics = Metrics()

    def on_call(outcome: CallOutcome) -> None:
        metrics.tool_calls.labels(outcome.status, outcome.decision).inc()
        if outcome.status == "error":
            metrics.errors.labels(f"tool_{(outcome.error or {}).get('code', 'error')}").inc()

    rt = runtime or Runtime(settings, on_call=on_call)
    if runtime is not None:
        runtime.gateway._on_call = on_call
    state = ApiState(settings, rt, metrics)
    gateway_app = create_gateway_app(rt.gateway)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.agent.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
        async with (
            AsyncSqliteSaver.from_conn_string(str(settings.agent.checkpoint_db)) as saver,
            gateway_app.state.session_manager.run(),
        ):
            if rt.runner is None:
                rt.attach_checkpointer(saver)
            supervisor = None
            if isinstance(rt.envs, LocalEnvService):
                install_cleanup(rt.envs.manager)
                supervisor = asyncio.create_task(rt.envs.manager.run_supervisor())
            try:
                yield
            finally:
                if supervisor is not None:
                    supervisor.cancel()
                await rt.aclose()

    app = FastAPI(title="BizAgent Workbench API", lifespan=lifespan)
    app.state.api = state
    app.mount("/gateway", gateway_app)  # the gateway's MCP endpoint: /gateway/mcp

    @app.middleware("http")
    async def timing(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        metrics.request_latency.labels(request.method, path, str(response.status_code)).observe(
            time.perf_counter() - started
        )
        return response

    def _session(sid: str) -> SessionInfo:
        if sid not in rt.sessions:
            raise HTTPException(404, f"unknown session {sid}")
        return rt.sessions[sid]

    def _observe(sid: str) -> Callable[[dict[str, Any]], None]:
        def hook(event: dict[str, Any]) -> None:
            if event.get("type") == "approval_required":
                state.approval_asked_at[sid] = time.monotonic()
            elif event.get("type") == "terminated":
                metrics.errors.labels(f"terminated_{event.get('reason')}").inc()

        return hook

    def _claim(sid: str) -> None:
        # Marked synchronously in the handler, so two concurrent requests cannot both pass.
        if sid in state.busy:
            raise HTTPException(409, "a turn is already running for this session")
        state.busy.add(sid)

    async def _turn(sid: str, events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        try:
            async for e in events:
                yield e
        finally:  # also runs when the client disconnects (the stream task is cancelled)
            state.busy.discard(sid)

    # ------------------------------------------------------------------ sessions
    @app.post("/sessions", response_model=SessionOut)
    async def create_session(body: CreateSession) -> SessionOut:
        if len(rt.sessions) >= settings.api.max_sessions:
            metrics.errors.labels("session_limit").inc()
            raise HTTPException(503, f"session limit reached ({settings.api.max_sessions})")
        try:
            info = await rt.create_session(body.scenario, body.allowlist)
        except EnvQueueTimeoutError as exc:
            metrics.errors.labels("env_capacity").inc()
            raise HTTPException(503, str(exc)) from exc
        except (EnvError, ValueError) as exc:
            metrics.errors.labels("env_start").inc()
            raise HTTPException(500, str(exc)[:500]) from exc
        if settings.llm.mock_reset_per_session and hasattr(rt.llm.backend, "reset"):
            rt.llm.backend.reset()
        metrics.active_envs.set(len(rt.sessions))
        return SessionOut(
            session_id=info.session_id,
            scenario=info.scenario,
            env_url=info.env.url,
            thread_id=info.thread_id,
            tools=info.tools,
        )

    @app.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [
            {"session_id": s.session_id, "scenario": s.scenario, "env_url": s.env.url, "tools": len(s.tools)}
            for s in rt.sessions.values()
        ]

    @app.delete("/sessions/{sid}")
    async def delete_session(sid: str) -> dict[str, str]:
        _session(sid)
        await rt.close_session(sid)
        metrics.active_envs.set(len(rt.sessions))
        return {"closed": sid}

    @app.post("/sessions/{sid}/messages")
    async def post_message(sid: str, body: MessageIn) -> Any:
        info = _session(sid)
        if rt.runner is None:
            raise HTTPException(503, "agent not ready")
        if await rt.runner.pending_approval(info.thread_id) is not None:
            raise HTTPException(409, "an approval is pending for this session; POST /approvals/{id} first")
        _claim(sid)
        await rt.envs.touch(sid)
        events = rt.runner.run(info.thread_id, sid, body.content, user_id=body.user_id)
        return to_sse(_turn(sid, events), on_event=_observe(sid))

    @app.get("/sessions/{sid}/trace")
    async def trace(sid: str) -> list[dict[str, Any]]:
        return rt.hub.events(sid)

    @app.get("/sessions/{sid}/diff")
    async def diff(sid: str, against: str = "initial") -> dict[str, Any]:
        _session(sid)
        try:
            return await rt.envs.diff(sid, against)
        except (EnvNotFoundError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/sessions/{sid}/tools")
    async def tools(sid: str) -> list[dict[str, Any]]:
        _session(sid)
        return rt.gateway.risk_table(sid)

    # ------------------------------------------------------------------ approvals
    @app.get("/approvals")
    async def list_approvals() -> list[dict[str, Any]]:
        out = []
        for s in rt.sessions.values():
            if rt.runner is not None and (pending := await rt.runner.pending_approval(s.thread_id)):
                out.append({"approval_id": s.session_id, **pending})
        return out

    @app.post("/approvals/{sid}")
    async def decide(sid: str, body: ApprovalIn) -> Any:
        info = _session(sid)
        pending = await rt.runner.pending_approval(info.thread_id) if rt.runner is not None else None
        if rt.runner is None or pending is None:
            raise HTTPException(404, "no pending approval for this session")
        preview = pending.get("preview") or {}
        if body.approved and preview and not preview.get("approvable", True):
            # approval.require_preview is on for this risk level and the preview failed (ADR-029)
            raise HTTPException(
                409,
                f"a {pending.get('risk')} call needs a successful preview before approval; "
                f"the preview failed ({preview.get('error')}); only a rejection is possible",
            )
        _claim(sid)
        asked = state.approval_asked_at.pop(sid, None)
        if asked is not None:
            metrics.approval_wait.observe(time.monotonic() - asked)
        decision = body.model_dump()
        return to_sse(_turn(sid, rt.runner.resume(info.thread_id, sid, decision)), on_event=_observe(sid))

    # ------------------------------------------------------------------ memory
    @app.get("/memory")
    async def get_memory(user_id: str = "default") -> list[dict[str, Any]]:
        return [m.as_dict() for m in rt.memory.list(user_id)]

    @app.delete("/memory/{key}")
    async def delete_memory(key: str, user_id: str = "default") -> dict[str, Any]:
        if not rt.memory.delete(user_id, key):
            raise HTTPException(404, f"no memory {key!r}")
        return {"deleted": key}

    # ------------------------------------------------------------------ ops
    @app.get("/scenarios")
    async def scenarios() -> list[dict[str, Any]]:
        return [
            {"name": s.name, "tools": s.tools, "tasks": s.tasks, "tables": s.tables}
            for s in build_catalog(settings.env.dataset_dir)
        ]

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "sessions": len(rt.sessions), "llm_backend": settings.llm.backend}

    @app.get("/metrics")
    async def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    ui_dir = Path(settings.api.ui_dir)
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    return app
