"""`workbench env serve`: HTTP control plane over an in-process EnvManager.

Used by the env-manager container in docker compose and by the `workbench env up/down/ls/
logs` CLI commands (which need a long-lived process to own the environment subprocesses).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from workbench.config import EnvSettings
from workbench.envs.catalog import build_catalog, search
from workbench.envs.manager import (
    EnvError,
    EnvManager,
    EnvNotFoundError,
    EnvQueueTimeoutError,
    install_cleanup,
)
from workbench.envs.service import EnvInfo


class StartRequest(BaseModel):
    scenario: str
    session_id: str | None = None


class NameRequest(BaseModel):
    name: str


def create_env_app(settings: EnvSettings, manager: EnvManager | None = None) -> FastAPI:
    mgr = manager or EnvManager(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        install_cleanup(mgr)
        supervisor = asyncio.create_task(mgr.run_supervisor())
        try:
            yield
        finally:
            supervisor.cancel()
            await mgr.stop_all()

    app = FastAPI(title="workbench env-manager", lifespan=lifespan)
    app.state.manager = mgr

    def _get(sid: str) -> Any:
        try:
            return mgr.get(sid)
        except EnvNotFoundError as exc:
            raise HTTPException(404, f"unknown session {sid}") from exc

    @app.post("/envs")
    async def start(req: StartRequest) -> dict[str, Any]:
        try:
            handle = await mgr.start(req.scenario, req.session_id)
        except EnvQueueTimeoutError as exc:
            raise HTTPException(503, str(exc)) from exc
        except EnvError as exc:
            raise HTTPException(500, str(exc)) from exc
        return EnvInfo.from_handle(handle).as_dict()

    @app.get("/envs")
    async def list_envs() -> list[dict[str, Any]]:
        return [EnvInfo.from_handle(h).as_dict() for h in mgr.list_envs()]

    @app.get("/envs/{sid}")
    async def info(sid: str) -> dict[str, Any]:
        mgr.refresh()
        return EnvInfo.from_handle(_get(sid)).as_dict()

    @app.delete("/envs/{sid}")
    async def stop(sid: str) -> dict[str, Any]:
        _get(sid)
        return EnvInfo.from_handle(await mgr.stop(sid)).as_dict()

    @app.get("/envs/{sid}/logs")
    async def logs(sid: str, lines: int = 200) -> dict[str, str]:
        _get(sid)
        return {"logs": mgr.logs(sid, lines)}

    @app.get("/envs/{sid}/diff")
    async def diff(sid: str, against: str = "initial") -> dict[str, Any]:
        _get(sid)
        return mgr.diff(sid, against).as_dict()

    @app.post("/envs/{sid}/snapshots")
    async def snapshot(sid: str, req: NameRequest) -> dict[str, str]:
        _get(sid)
        return {"path": str(mgr.snapshot(sid, req.name))}

    @app.post("/envs/{sid}/restore")
    async def restore(sid: str, req: NameRequest) -> dict[str, str]:
        _get(sid)
        mgr.restore(sid, req.name)
        return {"restored": req.name}

    @app.post("/envs/{sid}/touch")
    async def touch(sid: str) -> dict[str, str]:
        _get(sid)
        mgr.touch(sid)
        return {"ok": sid}

    @app.get("/envs/{sid}/health")
    async def health(sid: str) -> dict[str, Any]:
        _get(sid)
        r = await mgr.check(sid)
        return {"ok": r.ok, "tools": r.tools, "error": r.error}

    @app.get("/catalog")
    async def catalog(q: str | None = None) -> list[dict[str, Any]]:
        cat = build_catalog(settings.dataset_dir)
        rows = search(cat, q) if q else cat
        return [{"name": s.name, "tools": s.tools, "tasks": s.tasks, "tables": s.tables} for s in rows]

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "active": len(mgr.active)}

    return app
