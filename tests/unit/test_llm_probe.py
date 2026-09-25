"""`workbench serve probe` against a fake vLLM service (U1): its verdicts and the requests it sends.

The command runs in a subprocess: the text probe imports AWM, whose import chain loads sklearn and
sets KMP_* variables, and that must not leak into this test process. No model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.unit.synth_harness import free_port
from workbench.synth.runner import ProxyThread

MODEL = "Snowflake/Arctic-AWM-4B"
THINK = "<think>\nThe user asks about something; a tool can answer it.\n</think>\n\n"
LIST_TOOLS_TEXT = '<tool_call>\n{"name": "list_tools", "arguments": null}\n</tool_call>'
ORDER_TEXT = '<tool_call>\n{"name": "get_order_status", "arguments": {"order_id": 1024}}\n</tool_call>'


def sse(deltas: list[dict[str, Any]], finish: str) -> str:
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    chunks.append({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 5}})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def fake_vllm(seen: list[dict[str, Any]], *, parser_ok: bool, reject_tools: bool = False) -> Starlette:
    """Answers like vLLM with the hermes parser (``parser_ok``), or like a service that has no
    parser for native requests and a parser that takes calls out of requests without tools;
    ``reject_tools``: like vLLM started without the tool flags (render/serving.py:205-215)."""

    async def models(request: Request) -> JSONResponse:
        return JSONResponse({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    async def chat(request: Request) -> Response:
        body = json.loads(await request.body())
        seen.append(body)
        if body.get("tools") and reject_tools:
            message = '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
            error = {"message": message, "type": "BadRequestError", "param": None, "code": 400}
            return JSONResponse({"error": error}, status_code=400)
        if body.get("tools"):  # the workbench agent's act request, streamed
            if parser_ok:
                call = {"index": 0, "id": "chatcmpl-tool-1", "type": "function"}
                deltas = [
                    {"role": "assistant", "content": THINK},
                    {"tool_calls": [{**call, "function": {"name": "get_order_status", "arguments": ""}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": '{"order_id": 1024}'}}]},
                ]
                return Response(sse(deltas, "tool_calls"), media_type="text/event-stream")
            deltas = [{"role": "assistant", "content": THINK + ORDER_TEXT}]
            return Response(sse(deltas, "stop"), media_type="text/event-stream")
        if parser_ok:  # `awm agent`'s request: no tools, not streamed
            message = {"role": "assistant", "content": THINK + LIST_TOOLS_TEXT, "tool_calls": []}
        else:
            fn = {"name": "list_tools", "arguments": "null"}
            message = {
                "role": "assistant",
                "content": THINK,
                "tool_calls": [{"id": "t", "type": "function", "function": fn}],
            }
        choice = {"index": 0, "message": message, "finish_reason": "stop"}
        usage = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
        return JSONResponse(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [choice],
                "usage": usage,
            }
        )

    routes = [Route("/v1/models", models), Route("/v1/chat/completions", chat, methods=["POST"])]
    return Starlette(routes=routes)


def probe(base: str, *args: str) -> tuple[int, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/root"),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONPYCACHEPREFIX": str(Path(".cache/pycache").resolve()),
    }
    argv = [sys.executable, "-m", "workbench.cli", "serve", "probe", "--base-url", f"{base}/v1", *args]
    out = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120, check=False)
    return out.returncode, out.stdout + out.stderr


def test_probe_reports_native_calls_and_an_untouched_text_protocol() -> None:
    seen: list[dict[str, Any]] = []
    with ProxyThread(fake_vllm(seen, parser_ok=True), "127.0.0.1", free_port()) as base:
        rc, out = probe(base)
    assert rc == 0, out
    report = json.loads(out)
    assert report["served_models"] == [MODEL]
    native, text = report["native"], report["text"]
    assert native["verdict"] == "native" and native["finish_reason"] == "tool_calls"
    assert native["native_tool_calls"] == [{"name": "get_order_status", "arguments": '{"order_id": 1024}'}]
    assert native["think_open"] and native["think_close"] and not native["tool_call_text_in_content"]
    assert text["verdict"] == "ok" and text["native_tool_calls"] == []
    assert text["awm_parsed_tool_calls"] == [{"name": "list_tools", "arguments": None}]
    assert text["tool_call_text_in_content"]

    # the requests are the ones the two clients send (one each: no retries)
    act, awm = seen
    assert (
        act["model"] == MODEL
        and act["stream"] is True
        and act["tools"][0]["function"]["name"] == "get_order_status"
    )
    assert act["chat_template_kwargs"] == {"enable_thinking": True}  # the vllm backend's extra body
    assert "tools" not in awm and "stream" not in awm
    assert awm["max_completion_tokens"] == 2048 and awm["temperature"] == 1.0  # `awm agent` defaults
    assert awm["min_tokens"] == 16 and awm["add_generation_prompt"] is True  # its vLLM extras
    assert awm["chat_template_kwargs"] == {"enable_thinking": True}
    system = awm["messages"][0]
    assert (
        system["role"] == "system"
        and "<tool_call>" in system["content"]
        and "list_tools" in system["content"]
    )


def test_probe_flags_a_text_fallback_and_a_parser_that_interferes() -> None:
    seen: list[dict[str, Any]] = []
    with ProxyThread(fake_vllm(seen, parser_ok=False), "127.0.0.1", free_port()) as base:
        rc, out = probe(base)
    assert rc == 1, out  # the text protocol lost its call: that fails the probe
    report = json.loads(out)
    native, text = report["native"], report["text"]
    assert native["verdict"] == "text-fallback" and native["native_tool_calls"] == []
    assert native["client_tool_calls"] == [{"name": "get_order_status", "arguments": {"order_id": 1024}}]
    assert text["verdict"] == "parser-interfered" and text["awm_parsed_tool_calls"] == []


def test_probe_stops_when_the_model_is_not_served() -> None:
    seen: list[dict[str, Any]] = []
    with ProxyThread(fake_vllm(seen, parser_ok=True), "127.0.0.1", free_port()) as base:
        rc, out = probe(base, "--model", "some/other-model", "--only", "native")
        bad_rc, bad_out = probe(base, "--only", "judge")
    assert rc == 1 and "not served" in out and seen == []
    assert bad_rc == 2 and "--only" in bad_out
    rc, out = probe(f"http://127.0.0.1:{free_port()}")  # nothing listens there
    assert rc == 1 and "/models failed" in out


def test_probe_reports_a_service_started_without_the_tool_flags() -> None:
    seen: list[dict[str, Any]] = []
    with ProxyThread(fake_vllm(seen, parser_ok=True, reject_tools=True), "127.0.0.1", free_port()) as base:
        rc, out = probe(base, "--only", "native")
    report = json.loads(out)
    assert rc == 1 and report["native"]["verdict"] == "error" and "HTTP 400" in report["native"]["error"]
    assert "text" not in report and len(seen) == 1  # one attempt: the probe does not retry
