"""LLM client: two-layer timeouts, retry policy, token accounting (fake servers, no network)."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from workbench.config import LLMSettings
from workbench.llm.backends.mock_replay import MockReplayBackend
from workbench.llm.backends.openai_compat import OpenAICompatBackend
from workbench.llm.client import VLLM_EXTRA_BODY, LLMClient, build_backend
from workbench.llm.errors import LLMRequestError, LLMRetryableError, LLMTimeoutError
from workbench.llm.toolcall_parse import parse_tool_calls


def settings(**kw: Any) -> LLMSettings:
    base = {"connect_timeout_s": 1.0, "read_timeout_s": 0.5, "total_timeout_s": 5.0, "max_retries": 2}
    base.update(kw)
    return LLMSettings(**base)


def backend(
    url: str, s: LLMSettings, transport: httpx.AsyncBaseTransport | None = None, stream: bool = True
) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        name="openai_compat",
        base_url=url,
        model="m",
        api_key="k",
        connect_timeout_s=s.connect_timeout_s,
        read_timeout_s=s.read_timeout_s,
        stream=stream,
        transport=transport,
    )


class NoSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, d: float) -> None:
        self.delays.append(d)


# ------------------------------------------------------------------ raw socket servers
Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any]


async def _read_request(reader: asyncio.StreamReader) -> None:
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.decode().split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":")[1])
    await reader.readexactly(length)


async def serve(handler: Handler) -> AsyncIterator[tuple[str, list[int]]]:
    conns = [0]

    async def wrapped(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        conns[0] += 1
        try:
            await handler(r, w)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()

    server = await asyncio.start_server(wrapped, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1", conns
    finally:
        server.close()


async def test_slow_response_hits_read_timeout_and_is_retried() -> None:
    async def slow(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _read_request(r)
        await asyncio.sleep(3)  # never answers within read_timeout_s

    async for url, conns in serve(slow):
        s = settings(max_retries=1)
        sleeper = NoSleep()
        client = LLMClient(backend(url, s), s, sleep=sleeper)
        t0 = time.perf_counter()
        with pytest.raises(LLMRetryableError, match="ReadTimeout"):
            await client.chat([{"role": "user", "content": "hi"}])
        assert conns[0] == 2 and len(sleeper.delays) == 1  # one retry, then give up
        assert time.perf_counter() - t0 < 2.5
        await client.aclose()


async def test_half_open_stream_is_cut_by_wall_clock() -> None:
    async def trickle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _read_request(r)
        w.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n")
        first = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        w.write(b"%x\r\n%s\r\n" % (len(first), first))
        await w.drain()
        while True:  # keep the connection "alive": a comment line every 0.2s < read timeout
            await asyncio.sleep(0.2)
            w.write(b"2\r\n:\n\r\n")
            await w.drain()

    async for url, _ in serve(trickle):
        s = settings(read_timeout_s=0.5, total_timeout_s=1.5)
        client = LLMClient(backend(url, s), s, sleep=NoSleep())
        t0 = time.perf_counter()
        with pytest.raises(LLMTimeoutError):
            await client.chat([{"role": "user", "content": "hi"}])
        elapsed = time.perf_counter() - t0
        assert 1.4 < elapsed < 3.0  # read timeout alone would never fire
        await client.aclose()


async def test_connection_refused_retried_then_raised() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    s = settings(max_retries=2)
    sleeper = NoSleep()
    client = LLMClient(backend(f"http://127.0.0.1:{port}/v1", s), s, sleep=sleeper, rand=lambda: 0.0)
    with pytest.raises(LLMRetryableError, match="ConnectError"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert sleeper.delays == [0.25, 0.5]  # exponential: base 0.5 * 2**n, jitter factor 0.5
    await client.aclose()


# ------------------------------------------------------------------ MockTransport cases
def _json_transport(responses: list[httpx.Response], seen: list[dict[str, Any]]) -> httpx.MockTransport:
    it = iter(responses)

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return next(it)

    return httpx.MockTransport(handle)


COMPLETION = {
    "model": "m",
    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


async def test_5xx_retried_then_success_and_usage_recorded() -> None:
    seen: list[dict[str, Any]] = []
    t = _json_transport([httpx.Response(503, text="busy"), httpx.Response(200, json=COMPLETION)], seen)
    s = settings()
    events: list[dict[str, Any]] = []
    client = LLMClient(backend("http://x/v1", s, t, stream=False), s, sleep=NoSleep(), on_usage=events.append)
    r = await client.chat([{"role": "user", "content": "hi"}], purpose="plan")
    assert r.content == "hello" and len(seen) == 2
    assert client.usage.total_tokens == 10 and client.calls == 1
    assert events[0]["purpose"] == "plan" and events[0]["attempts"] == 2 and events[0]["prompt_tokens"] == 7


@pytest.mark.parametrize("status", [400, 401, 404, 429])
async def test_4xx_not_retried(status: int) -> None:
    seen: list[dict[str, Any]] = []
    t = _json_transport([httpx.Response(status, text="nope")], seen)
    s = settings()
    client = LLMClient(backend("http://x/v1", s, t, stream=False), s, sleep=NoSleep())
    with pytest.raises(LLMRequestError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert len(seen) == 1


async def test_stream_assembles_native_tool_calls_and_usage() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "function": {"name": "search_", "arguments": '{"query"'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"name": "products", "arguments": ': "x"}'}}]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    seen: list[dict[str, Any]] = []
    t = _json_transport([httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})], seen)
    s = settings()
    client = LLMClient(backend("http://x/v1", s, t), s)
    tools = [{"name": "search_products", "description": "d", "input_schema": {"type": "object"}}]
    r = await client.chat([{"role": "user", "content": "hi"}], tools)
    assert r.tool_calls[0].name == "search_products" and r.tool_calls[0].arguments == {"query": "x"}
    assert r.usage.total_tokens == 15
    assert seen[0]["tools"][0]["function"]["name"] == "search_products"
    assert seen[0]["stream_options"] == {"include_usage": True}


async def test_tool_call_inside_content_is_parsed() -> None:
    msg = {
        "choices": [
            {
                "message": {
                    "content": "<think>plan</think>ok <tool_call>"
                    '{"name": "add_item_to_cart", "arguments": {"product_id": 1, "quantity": 1}}'
                    "</tool_call>"
                },
                "finish_reason": "stop",
            }
        ]
    }
    t = _json_transport([httpx.Response(200, json=msg)], [])
    s = settings()
    r = await LLMClient(backend("http://x/v1", s, t, stream=False), s).chat(
        [{"role": "user", "content": "x"}]
    )
    assert [c.name for c in r.tool_calls] == ["add_item_to_cart"] and r.content == "ok"


def test_parse_tool_calls_edge_cases() -> None:
    _, calls = parse_tool_calls(
        '<tool_call>not json</tool_call><tool_call>{"name": "t", "arguments": "{\\"a\\": 1}"}</tool_call>'
    )
    assert [(c.name, c.arguments) for c in calls] == [("t", {"a": 1})]
    _, bad = parse_tool_calls('<tool_call>{"name": "t", "arguments": "{oops"}</tool_call>')
    assert bad[0].arguments == {"_raw_arguments": "{oops"}


def test_backend_selected_by_config_only() -> None:
    assert isinstance(build_backend(LLMSettings()), MockReplayBackend)
    vb = build_backend(LLMSettings(backend="vllm"))
    assert isinstance(vb, OpenAICompatBackend) and vb.extra_body == VLLM_EXTRA_BODY
    ob = build_backend(
        LLMSettings(backend="openai_compat", base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    )
    assert isinstance(ob, OpenAICompatBackend) and ob.extra_body == {} and ob.model == "deepseek-chat"


def test_serving_profile_command() -> None:
    from pathlib import Path

    from workbench.llm.serving import ServingProfile, vllm_command

    p = ServingProfile.load(Path("configs/serving/arctic-awm-4b.yaml"))
    cmd = vllm_command(p)
    assert cmd[:3] == ["vllm", "serve", "Snowflake/Arctic-AWM-4B"]
    # ADR-020 (owner decision D11): the repo profile enables the hermes tool parser, no reasoning parser
    assert cmd[-3:] == ["--enable-auto-tool-choice", "--tool-call-parser", "hermes"]
    assert "--reasoning-parser" not in cmd and "--chat-template" not in cmd
    with pytest.raises(ValueError):
        vllm_command(ServingProfile(model="m", enable_auto_tool_choice=True))
    full = vllm_command(
        ServingProfile(
            model="m",
            enable_auto_tool_choice=True,
            tool_call_parser="hermes",
            reasoning_parser="qwen3",
            max_model_len=8192,
        )
    )
    assert full[-7:] == [
        "--max-model-len",
        "8192",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
        "--reasoning-parser",
        "qwen3",
    ]


def test_compose_vllm_matches_the_serving_profile() -> None:
    """D16: docker-compose's vllm service must not drift from the serving profile (ADR-020)."""
    from pathlib import Path

    import yaml

    from workbench.llm.serving import ServingProfile, vllm_command

    profile = ServingProfile.load(Path("configs/serving/arctic-awm-4b.yaml"))
    expected = vllm_command(profile)[1:]  # the compose entrypoint is `vllm`
    expected[expected.index("--host") + 1] = "0.0.0.0"  # reachable from the other containers
    service = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))["services"]["vllm"]
    assert service["entrypoint"] == ["vllm"]
    assert service["command"] == expected
    assert service["image"] == "vllm/vllm-openai:v0.19.0"  # the version the profile was checked against
