"""reasoning_content pass-back (ADR-017), checked against a fake DeepSeek server.

The fake server follows the rules of https://api-docs.deepseek.com/guides/thinking_mode
(read 2026-09-24): replies carry ``reasoning_content``; in strict mode a request with ``tools``
whose earlier assistant turns lack their ``reasoning_content`` gets HTTP 400.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.unit.agent_harness import SCENARIO, MiniUpstream, collect
from workbench.agent.deps import AgentDeps
from workbench.agent.memory import MemoryService
from workbench.agent.runner import AgentRunner
from workbench.config import AgentSettings, GatewaySettings, LLMSettings
from workbench.gateway.core import Gateway
from workbench.gateway.policy import ApprovalService, PolicyConfig
from workbench.llm.backends.openai_compat import OpenAICompatBackend
from workbench.llm.client import LLMClient
from workbench.llm.reasoning import ReasoningStore
from workbench.llm.types import ChatResult, ToolCall
from workbench.obs.tracing import TraceHub

TOOLS = [{"name": "search_products", "description": "Search", "input_schema": {"type": "object"}}]


class FakeDeepSeek:
    """Scripted replies; records every request body; ``strict`` enforces the documented 400."""

    def __init__(self, replies: list[dict[str, Any]], *, strict: bool = False, stream: bool = True) -> None:
        self.replies = list(replies)
        self.strict = strict
        self.stream = stream
        self.requests: list[dict[str, Any]] = []
        self.produced: dict[str, str] = {}  # reply content/tool-call id -> reasoning it came with

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(copy.deepcopy(body))
        if self.strict and body.get("tools"):
            for m in body["messages"]:
                if m.get("role") != "assistant":
                    continue
                calls = m.get("tool_calls") or []
                key = calls[0]["id"] if calls else m.get("content") or ""
                if key in self.produced and m.get("reasoning_content") != self.produced[key]:
                    return httpx.Response(400, json={"error": {"message": "reasoning_content missing"}})
        reply = self.replies.pop(0)
        calls = reply.get("tool_calls") or []
        self.produced[calls[0]["id"] if calls else reply.get("content") or ""] = reply.get("reasoning", "")
        return self._stream(reply) if self.stream else self._completion(reply)

    @staticmethod
    def _completion(reply: dict[str, Any]) -> httpx.Response:
        message: dict[str, Any] = {"role": "assistant", "content": reply.get("content", "")}
        if reply.get("reasoning"):
            message["reasoning_content"] = reply["reasoning"]
        if reply.get("tool_calls"):
            message["tool_calls"] = reply["tool_calls"]
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": message, "finish_reason": "stop"}]}
        )

    @staticmethod
    def _stream(reply: dict[str, Any]) -> httpx.Response:
        chunks: list[dict[str, Any]] = []
        if reply.get("reasoning"):
            half = len(reply["reasoning"]) // 2
            for part in (reply["reasoning"][:half], reply["reasoning"][half:]):
                chunks.append({"choices": [{"delta": {"reasoning_content": part}}]})
        if reply.get("content"):
            chunks.append({"choices": [{"delta": {"content": reply["content"]}}]})
        for i, tc in enumerate(reply.get("tool_calls") or []):
            chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, **tc}]}}]})
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": None})
        chunks.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def tool_call(cid: str, name: str = "search_products", args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


def backend(fake: FakeDeepSeek) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        name="openai_compat",
        base_url="http://deepseek",
        model="m",
        api_key="k",
        connect_timeout_s=1.0,
        read_timeout_s=1.0,
        stream=fake.stream,
        transport=fake.transport(),
    )


def assistant_turn(result: ChatResult) -> dict[str, Any]:
    """The assistant message the act node appends (agent/nodes/act.py): content + first tool call."""
    msg: dict[str, Any] = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        tc = result.tool_calls[0]
        msg["tool_calls"] = [tool_call(tc.id, tc.name, tc.arguments)]
    return msg


@pytest.mark.parametrize("stream", [True, False])
async def test_reasoning_content_is_captured(stream: bool) -> None:
    fake = FakeDeepSeek([{"reasoning": "think first", "tool_calls": [tool_call("c1")]}], stream=stream)
    b = backend(fake)
    r = await b.chat([{"role": "user", "content": "hi"}], TOOLS)
    assert r.reasoning_content == "think first" and r.tool_calls[0].id == "c1"
    await b.aclose()


async def test_passed_back_on_tool_requests_only() -> None:
    fake = FakeDeepSeek(
        [
            {"reasoning": "R1", "tool_calls": [tool_call("c1")]},
            {"reasoning": "R2", "content": "done"},
            {"content": "no tools"},
        ]
    )
    b = backend(fake)
    history: list[dict[str, Any]] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    r1 = await b.chat(history, TOOLS)
    history += [assistant_turn(r1), {"role": "tool", "tool_call_id": "c1", "content": "{}"}]
    snapshot = copy.deepcopy(history)
    await b.chat(history, TOOLS)
    sent = [m for m in fake.requests[1]["messages"] if m["role"] == "assistant"]
    assert sent[0]["reasoning_content"] == "R1"
    assert history == snapshot  # the caller's (LangGraph state) dicts are not modified
    await b.chat(history, None)  # no tools: not needed, not sent
    assert all("reasoning_content" not in m for m in fake.requests[2]["messages"])
    await b.aclose()


async def test_final_answer_turn_and_replanned_system_prompt() -> None:
    fake = FakeDeepSeek(
        [
            {"reasoning": "R1", "tool_calls": [tool_call("c1")]},
            {"reasoning": "R2", "content": "answer"},
            {"content": "again"},
        ]
    )
    b = backend(fake)
    history: list[dict[str, Any]] = [
        {"role": "system", "content": "plan v1"},
        {"role": "user", "content": "q"},
    ]
    r1 = await b.chat(history, TOOLS)
    history += [assistant_turn(r1), {"role": "tool", "tool_call_id": "c1", "content": "{}"}]
    r2 = await b.chat(history, TOOLS)
    # verify said "incomplete": the final answer turn stays in history and act runs again
    history += [assistant_turn(r2), {"role": "user", "content": "Verification: still missing ..."}]
    history[0] = {"role": "system", "content": "plan v2"}  # re-planning rebuilds the system prompt
    await b.chat(history, TOOLS)
    sent = [m.get("reasoning_content") for m in fake.requests[2]["messages"] if m["role"] == "assistant"]
    assert sent == ["R1", "R2"]  # also the turn without a tool call
    await b.aclose()


async def test_unknown_turns_and_caller_reasoning_are_left_alone() -> None:
    fake = FakeDeepSeek([{"content": "ok"}])
    b = backend(fake)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "from another backend"},
        {"role": "assistant", "content": "mine", "reasoning_content": "caller kept it"},
    ]
    await b.chat(messages, TOOLS)
    sent = [m for m in fake.requests[0]["messages"] if m["role"] == "assistant"]
    assert "reasoning_content" not in sent[0] and sent[1]["reasoning_content"] == "caller kept it"
    await b.aclose()


async def test_servers_without_reasoning_see_no_new_field() -> None:
    fake = FakeDeepSeek([{"tool_calls": [tool_call("c1")]}, {"content": "done"}])
    b = backend(fake)
    history: list[dict[str, Any]] = [{"role": "user", "content": "q"}]
    r1 = await b.chat(history, TOOLS)
    assert r1.reasoning_content is None and len(b.reasoning) == 0
    history += [assistant_turn(r1), {"role": "tool", "tool_call_id": "c1", "content": "{}"}]
    await b.chat(history, TOOLS)
    assert all("reasoning_content" not in m for m in fake.requests[1]["messages"])
    await b.aclose()


def test_store_is_bounded() -> None:
    store = ReasoningStore(max_entries=2)
    for i in range(3):
        store.remember([{"role": "user", "content": str(i)}], ChatResult(f"a{i}", reasoning_content=f"r{i}"))
    assert len(store) == 2
    oldest = store.attach([{"role": "user", "content": "0"}, {"role": "assistant", "content": "a0"}])
    newest = store.attach([{"role": "user", "content": "2"}, {"role": "assistant", "content": "a2"}])
    assert "reasoning_content" not in oldest[1] and newest[1]["reasoning_content"] == "r2"


def test_tool_turn_identity_uses_the_first_tool_call_only() -> None:
    store = ReasoningStore()
    result = ChatResult("", [ToolCall("c1", "a", {}), ToolCall("c2", "b", {})], reasoning_content="R")
    store.remember([{"role": "user", "content": "q"}], result)
    kept_first = {"role": "assistant", "content": "", "tool_calls": [tool_call("c1", "a")]}
    assert store.attach([{"role": "user", "content": "q"}, kept_first])[1]["reasoning_content"] == "R"


async def test_agent_run_against_a_server_enforcing_the_documented_400(tmp_path: Path) -> None:
    plan = {
        "steps": [{"description": "search", "tool": f"{SCENARIO}__search_products", "changes_data": False}]
    }
    fake = FakeDeepSeek(
        [
            {"reasoning": "plan it", "content": json.dumps(plan)},
            {
                "reasoning": "search first",
                "tool_calls": [tool_call("c1", f"{SCENARIO}__search_products", {"query": "headphones"})],
            },
            {"reasoning": "summarize", "content": "Headphones A is rated best."},
            {"reasoning": "check", "content": json.dumps({"complete": True, "missing": [], "memories": []})},
        ],
        strict=True,
    )
    gateway = Gateway(
        GatewaySettings(audit_path=tmp_path / "audit.jsonl"),
        policy=PolicyConfig.load(Path("configs/tool_policy.yaml")),
        approvals=ApprovalService(secret=b"k"),
        upstream=MiniUpstream(),
    )
    await gateway.register_session("s1", SCENARIO, "http://fake/mcp")
    deps = AgentDeps(
        llm=LLMClient(backend(fake), LLMSettings(backend="openai_compat")),
        gateway=gateway,
        settings=AgentSettings(),
        hub=TraceHub(tmp_path / "runs"),
        memory=MemoryService(tmp_path / "memory.sqlite", ttl_s=3600),
    )
    from langgraph.checkpoint.memory import InMemorySaver

    events = await collect(
        AgentRunner(deps, InMemorySaver()).run("t1", "s1", "Which headphones are rated best?")
    )
    done = events[-1]
    assert done["type"] == "done" and done["termination"] is None
    assert done["final_answer"] == "Headphones A is rated best."
    second_act = fake.requests[2]
    assert second_act.get("tools")
    turns = [m for m in second_act["messages"] if m["role"] == "assistant"]
    assert [t.get("reasoning_content") for t in turns] == ["search first"]
