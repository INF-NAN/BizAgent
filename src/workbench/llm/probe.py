"""`workbench serve probe`: how the vLLM service answers the two kinds of tool-calling request (U1).

One request of each kind to the served model (a local service, no paid API). Each is a check of the
serving chain, run once; nothing here is an evaluation.

- ``native``: the request the workbench agent sends when it acts, built and sent by the ``vllm``
  backend itself (native ``tools``, streaming, thinking on). Behind ``--enable-auto-tool-choice
  --tool-call-parser hermes`` (ADR-020) the answer should carry native ``tool_calls``. Without
  them the backend falls back to reading ``<tool_call>`` text from ``content``
  (``llm/toolcall_parse.py``); the probe reports that as ``text-fallback``.
- ``text``: the first request `awm agent` sends to a local vLLM, made by AWM's own
  ``generate_response`` (awm/core/agent.py:339-386) with its system prompt (:88-127) and the vLLM
  extras it adds for a ``localhost`` URL (:374-379, :417). The request has no ``tools``, so the
  parser must leave the ``<tool_call>`` text in ``content``, where AWM's parser (:130-167) finds
  it. Native ``tool_calls`` in this answer mean the parser interfered.

Both probes also keep the raw response body (an httpx response hook) to report what the service
itself sent, apart from what each client made of it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from workbench.config import LLMSettings
from workbench.llm.backends.openai_compat import OpenAICompatBackend
from workbench.llm.client import build_backend
from workbench.llm.errors import LLMError
from workbench.llm.types import Message

PROBES = ("native", "text")

TOOL: dict[str, Any] = {
    "name": "get_order_status",
    "description": "Look up the current status of an order by its id.",
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "integer", "description": "The order id."}},
        "required": ["order_id"],
    },
}
NATIVE_MESSAGES: list[Message] = [
    {"role": "system", "content": "You help customers by calling the available tools."},
    {"role": "user", "content": "What is the status of order 1024?"},
]
TEXT_TASK = "Show me the three most recent orders in my account."


class ProbeError(RuntimeError):
    """The service is unreachable or does not serve the model."""


def _keep_bodies(bodies: list[bytes]) -> Callable[[httpx.Response], Awaitable[None]]:
    async def keep(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/chat/completions"):
            bodies.append(await response.aread())

    return keep


def _content_facts(content: str) -> dict[str, Any]:
    return {
        "content_chars": len(content),
        "think_open": "<think>" in content,
        "think_close": "</think>" in content,
        "tool_call_text_in_content": "<tool_call>" in content,
        "content_head": content[:160],
        "content_tail": content[-240:],
    }


def _read_stream(body: bytes) -> tuple[str, list[dict[str, str]], str | None, dict[str, Any]]:
    """Content, native tool calls, finish reason and usage of a streamed answer."""
    content: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    finish: str | None = None
    usage: dict[str, Any] = {}
    for line in body.decode("utf-8", errors="replace").splitlines():
        data = line[5:].strip() if line.startswith("data:") else ""
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", 0)), {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    return "".join(content), [calls[i] for i in sorted(calls)], finish, usage


def _read_json(body: bytes) -> tuple[str, list[dict[str, str]], str | None, dict[str, Any]]:
    """The same for an answer that was not streamed."""
    raw = json.loads(body)
    choice = raw["choices"][0]
    message = choice["message"]
    calls = [
        {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or ""}
        for tc in message.get("tool_calls") or []
    ]
    return message.get("content") or "", calls, choice.get("finish_reason"), raw.get("usage") or {}


async def probe_native(settings: LLMSettings) -> dict[str, Any]:
    """The act request of the workbench agent, through the ``vllm`` backend."""
    backend = build_backend(settings.model_copy(update={"backend": "vllm"}))
    assert isinstance(backend, OpenAICompatBackend)
    bodies: list[bytes] = []
    backend._client.event_hooks = {"request": [], "response": [_keep_bodies(bodies)]}
    started = time.perf_counter()
    try:
        result = await backend.chat(
            NATIVE_MESSAGES, [TOOL], temperature=settings.temperature, max_tokens=settings.max_tokens
        )
    finally:
        await backend.aclose()
    seconds = round(time.perf_counter() - started, 1)
    content, native, finish, usage = (_read_stream if settings.stream else _read_json)(bodies[-1])
    verdict = "native" if native else ("text-fallback" if result.tool_calls else "no-call")
    return {
        "verdict": verdict,
        "native_tool_calls": native,
        "client_tool_calls": [{"name": c.name, "arguments": c.arguments} for c in result.tool_calls],
        "finish_reason": finish,
        "usage": usage,
        "seconds": seconds,
        **_content_facts(content),
    }


async def probe_text(settings: LLMSettings) -> dict[str, Any]:
    """The first request of `awm agent`, made by AWM's own code."""
    from awm.core.agent import Config, generate_response, get_system_prompt
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    bodies: list[bytes] = []
    # the SDK's own client defaults (what AWM gets), plus the hook
    http = DefaultAsyncHttpxClient(event_hooks={"request": [], "response": [_keep_bodies(bodies)]})
    # as awm/core/agent.py:472-475 (key: awm/tools.py:429), but one attempt: no SDK retries
    client = AsyncOpenAI(
        base_url=settings.base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
        http_client=http,
        max_retries=0,
    )
    messages = [{"role": "system", "content": get_system_prompt()}, {"role": "user", "content": TEXT_TASK}]
    started = time.perf_counter()
    try:
        _, awm_calls = await generate_response(
            client, settings.model, messages, Config(), use_vllm_extras=True
        )
    finally:
        await client.close()
        await http.aclose()
    seconds = round(time.perf_counter() - started, 1)
    content, native, finish, usage = _read_json(bodies[-1])
    verdict = "parser-interfered" if native else ("ok" if awm_calls else "no-call")
    return {
        "verdict": verdict,
        "native_tool_calls": native,
        "awm_parsed_tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in awm_calls],
        "finish_reason": finish,
        "usage": usage,
        "seconds": seconds,
        **_content_facts(content),
    }


PROBE_FUNCS: dict[str, Callable[[LLMSettings], Awaitable[dict[str, Any]]]] = {
    "native": probe_native,
    "text": probe_text,
}


async def served_models(settings: LLMSettings) -> list[str]:
    key = os.environ.get(settings.api_key_env)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_s)) as http:
        resp = await http.get(settings.base_url.rstrip("/") + "/models", headers=headers)
        resp.raise_for_status()
        return [str(m["id"]) for m in resp.json().get("data") or []]


async def run_probe(settings: LLMSettings, which: Sequence[str] = PROBES) -> dict[str, Any]:
    """Check that the model is served, then send each probe once."""
    from openai import OpenAIError

    report: dict[str, Any] = {"base_url": settings.base_url, "model": settings.model}
    try:
        report["served_models"] = await served_models(settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise ProbeError(f"{settings.base_url}/models failed: {type(exc).__name__}: {exc}") from exc
    if settings.model not in report["served_models"]:
        raise ProbeError(f"{settings.model} is not served here (served: {report['served_models']})")
    for name in which:
        try:
            report[name] = await PROBE_FUNCS[name](settings)
        except (LLMError, OpenAIError, httpx.HTTPError, LookupError, ValueError) as exc:
            report[name] = {"verdict": "error", "error": f"{type(exc).__name__}: {exc}"}
    return report


def failed(report: dict[str, Any]) -> bool:
    """A probe could not run, or the parser took tool calls out of a request without ``tools``."""
    return any(report.get(n, {}).get("verdict") in ("error", "parser-interfered") for n in PROBES)
