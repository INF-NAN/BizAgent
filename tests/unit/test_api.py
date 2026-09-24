"""API tests (mock LLM, fake env service, real gateway with a fake upstream)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from tests.unit.agent_harness import FIX, MiniUpstream
from workbench.api.app import create_app
from workbench.config import Settings
from workbench.envs.service import EnvInfo
from workbench.gateway.core import Gateway
from workbench.gateway.policy import ApprovalService, PolicyConfig
from workbench.llm.backends.mock_replay import MockReplayBackend
from workbench.llm.client import LLMClient
from workbench.llm.types import ChatResult, Message
from workbench.runtime import Runtime


class FakeEnvService:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo:
        sid = session_id or "x"
        self.started.append(sid)
        return EnvInfo(sid, scenario, "http://fake/mcp", "healthy", 0, [])

    async def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)

    async def info(self, session_id: str) -> EnvInfo:
        return EnvInfo(session_id, "mini_e_commerce", "http://fake/mcp", "healthy", 0, [])

    async def list_envs(self) -> list[EnvInfo]:
        return []

    async def logs(self, session_id: str, lines: int = 200) -> str:
        return ""

    async def diff(self, session_id: str, against: str = "initial") -> dict[str, Any]:
        return {
            "changed": True,
            "tables": {
                "cart_items": {"rows_before": 1, "rows_after": 2, "added": [2], "removed": [], "changed": []}
            },
        }

    async def snapshot(self, session_id: str, name: str) -> None: ...
    async def restore(self, session_id: str, name: str) -> None: ...
    async def touch(self, session_id: str) -> None: ...
    async def close(self) -> None: ...


def make_settings(tmp_path: Path, **api: Any) -> Settings:
    return Settings(
        env={"dataset_dir": Path("tests/fixtures/awm_mini"), "runs_dir": tmp_path / "runs"},  # type: ignore[arg-type]
        gateway={"audit_path": tmp_path / "audit.jsonl"},  # type: ignore[arg-type]
        agent={"checkpoint_db": tmp_path / "ckpt.sqlite", "memory_db": tmp_path / "mem.sqlite"},  # type: ignore[arg-type]
        api={"max_sessions": 1, **api},  # type: ignore[arg-type]
    )


def make_app(tmp_path: Path, backend: Any = None, **api: Any) -> tuple[FastAPI, Runtime, FakeEnvService]:
    settings = make_settings(tmp_path, **api)
    envs = FakeEnvService()
    gateway = Gateway(
        settings.gateway,
        policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=MiniUpstream(),
    )
    llm = LLMClient(backend or MockReplayBackend(FIX / "demo_query_write_approve.jsonl"), settings.llm)
    rt = Runtime(settings, env_service=envs, gateway=gateway, llm=llm)
    return create_app(settings, runtime=rt), rt, envs


async def lifespan_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c,
    ):
        yield c


def sse_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


async def test_full_flow_over_http(tmp_path: Path) -> None:
    app, _, envs = make_app(tmp_path)
    async for c in lifespan_client(app):
        assert (await c.get("/healthz")).json()["ok"] is True
        scen = (await c.get("/scenarios")).json()
        assert scen[0]["name"] == "mini_e_commerce" and scen[0]["tools"] == 7

        r = await c.post("/sessions", json={"scenario": "mini_e_commerce"})
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert any(
            t["name"].endswith("add_item_to_cart") and t["requires_approval"] for t in r.json()["tools"]
        )
        # concurrency limit (max_sessions=1) -> 503
        assert (await c.post("/sessions", json={"scenario": "mini_e_commerce"})).status_code == 503

        r = await c.post(f"/sessions/{sid}/messages", json={"content": "buy the best headphones under $200"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = sse_events(r.text)
        assert events[-1]["type"] == "approval_required"
        assert any(e["type"] == "tool_call" for e in events)

        pending = (await c.get("/approvals")).json()
        assert pending[0]["approval_id"] == sid and pending[0]["tool"].endswith("add_item_to_cart")
        # a new message while an approval is pending is refused
        assert (await c.post(f"/sessions/{sid}/messages", json={"content": "hi"})).status_code == 409

        r = await c.post(f"/approvals/{sid}", json={"approved": True, "approver": "alice"})
        done = sse_events(r.text)[-1]
        assert done["type"] == "done" and "added" in done["final_answer"].lower()
        assert (
            await c.post(f"/approvals/{sid}", json={"approved": True, "approver": "x"})
        ).status_code == 404

        trace = (await c.get(f"/sessions/{sid}/trace")).json()
        assert {"session_created", "tool_call", "approval_granted", "final"} <= {e["type"] for e in trace}
        assert (await c.get(f"/sessions/{sid}/diff")).json()["tables"]["cart_items"]["added"] == [2]
        assert len((await c.get(f"/sessions/{sid}/tools")).json()) == 7

        mem = (await c.get("/memory")).json()
        assert [m["key"] for m in mem] == ["headphone_budget"]
        assert (await c.delete("/memory/headphone_budget")).status_code == 200
        assert (await c.delete("/memory/headphone_budget")).status_code == 404

        metrics = (await c.get("/metrics")).text
        assert 'workbench_tool_calls_total{decision="allowed",status="ok"} 2.0' in metrics
        assert "workbench_approval_wait_seconds_count 1.0" in metrics
        assert "workbench_active_envs 1.0" in metrics
        assert "workbench_http_request_duration_seconds_bucket" in metrics

        assert (await c.delete(f"/sessions/{sid}")).status_code == 200
        assert envs.stopped == [sid]
        assert (await c.get(f"/sessions/{sid}/trace")).status_code == 200
        assert (await c.post(f"/sessions/{sid}/messages", json={"content": "x"})).status_code == 404


async def test_ui_is_served(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    async for c in lifespan_client(app):
        r = await c.get("/ui/")
        assert r.status_code == 200 and "BizAgent Workbench" in r.text
        assert (await c.get("/ui/app.js")).status_code == 200


class SlowBackend:
    name = "slow"

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def chat(self, messages: list[Message], tools: Any = None, **kw: Any) -> ChatResult:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ChatResult("never")

    async def aclose(self) -> None: ...


async def test_client_disconnect_cancels_turn(tmp_path: Path) -> None:
    backend = SlowBackend()
    app, rt, _ = make_app(tmp_path, backend=backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
            sid = (await c.post("/sessions", json={"scenario": "mini_e_commerce"})).json()["session_id"]

        # Drive the ASGI app by hand so we can disconnect mid-stream.
        body = json.dumps({"content": "hello"}).encode()
        sent: list[dict[str, Any]] = []
        started = asyncio.Event()
        disconnect = asyncio.Event()
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict[str, Any]:
            if messages:
                return messages.pop()
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg: dict[str, Any]) -> None:
            sent.append(msg)
            if msg["type"] == "http.response.body" and msg.get("body"):
                started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/sessions/{sid}/messages",
            "raw_path": f"/sessions/{sid}/messages".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json"), (b"host", b"api")],
            "client": ("127.0.0.1", 1),
            "server": ("api", 80),
        }
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 5)  # intake/plan events are flowing
        assert sid in app.state.api.busy
        disconnect.set()
        await asyncio.wait_for(task, 5)  # the handler returns promptly after the disconnect
        await asyncio.wait_for(backend.cancelled.wait(), 5)  # ...and the in-flight LLM call was cancelled
        assert sid not in app.state.api.busy
        assert "cancelled" in {e["type"] for e in rt.hub.events(sid)}


async def test_runtime_passes_route_methods_to_the_gateway(tmp_path: Path) -> None:
    class MethodsEnv(FakeEnvService):
        async def start(self, scenario: str, session_id: str | None = None) -> EnvInfo:
            info = await super().start(scenario, session_id)
            return EnvInfo(
                info.session_id,
                scenario,
                info.url,
                "healthy",
                0,
                [],
                tool_methods={"list_cart_items": "DELETE"},
            )

    settings = make_settings(tmp_path)
    gateway = Gateway(
        settings.gateway,
        policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=MiniUpstream(),
    )
    rt = Runtime(
        settings,
        env_service=MethodsEnv(),
        gateway=gateway,
        llm=LLMClient(MockReplayBackend(FIX / "demo_query_write_approve.jsonl"), settings.llm),
    )
    s = await rt.create_session("mini_e_commerce")
    tools = {t["name"].split("__")[1]: t for t in s.tools}
    assert tools["list_cart_items"]["risk"] == "destructive" and tools["list_cart_items"]["requires_approval"]
    assert tools["search_products"]["risk"] == "read" and tools["search_products"]["http_method"] is None
    await rt.aclose()
