"""OpenAI-compatible chat backend (vLLM, DeepSeek, ...) over httpx.

Layer 1 of the two-layer timeout: httpx phase timeouts (connect / read / write / pool). A
read timeout alone cannot stop a stream that trickles one byte just before every deadline
("half-open" hang); layer 2 — the overall wall-clock cap — lives in ``LLMClient``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from workbench.llm.errors import LLMRequestError, LLMRetryableError
from workbench.llm.toolcall_parse import parse_arguments, parse_tool_calls, strip_think
from workbench.llm.types import ChatResult, Message, ToolCall, Usage


def _tools_payload(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


class OpenAICompatBackend:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None,
        connect_timeout_s: float,
        read_timeout_s: float,
        stream: bool = True,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.stream = stream
        self.extra_body = dict(extra_body or {})
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = httpx.Timeout(
            connect=connect_timeout_s, read=read_timeout_s, write=read_timeout_s, pool=connect_timeout_s
        )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": self.stream}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = _tools_payload(tools)
        if self.stream:
            payload["stream_options"] = {"include_usage": True}
        payload.update(self.extra_body)
        return payload

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload = self._payload(messages, tools, temperature, max_tokens)
        try:
            if self.stream:
                return await self._chat_stream(payload)
            resp = await self._client.post("/chat/completions", json=payload)
            _raise_for_status(resp.status_code, resp.text)
            return _from_completion(resp.json(), self.model)
        except httpx.TransportError as exc:  # connect/read/write/pool timeouts, resets, protocol errors
            raise LLMRetryableError(f"{type(exc).__name__}: {exc}") from exc

    async def _chat_stream(self, payload: dict[str, Any]) -> ChatResult:
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: str | None = None
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                _raise_for_status(resp.status_code, body)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = Usage(
                        int(chunk["usage"].get("prompt_tokens", 0)),
                        int(chunk["usage"].get("completion_tokens", 0)),
                    )
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            int(tc.get("index", 0)), {"id": None, "name": "", "arguments": ""}
                        )
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    finish = choice.get("finish_reason") or finish
        native = [
            ToolCall(str(c["id"] or f"call_{i}"), c["name"], parse_arguments(c["arguments"]))
            for i, c in sorted(calls.items())
        ]
        return _finalize("".join(content), native, usage, self.model, finish)


def _raise_for_status(status: int, body: str) -> None:
    if status >= 500:
        raise LLMRetryableError(f"HTTP {status}: {body[:300]}")
    if status >= 400:
        raise LLMRequestError(f"HTTP {status}: {body[:300]}")


def _from_completion(data: dict[str, Any], model: str) -> ChatResult:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"malformed completion: {str(data)[:200]}") from exc
    native = [
        ToolCall(
            str(tc.get("id") or f"call_{i}"),
            tc["function"]["name"],
            parse_arguments(tc["function"].get("arguments")),
        )
        for i, tc in enumerate(msg.get("tool_calls") or [])
    ]
    u = data.get("usage") or {}
    usage = Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
    return _finalize(
        msg.get("content") or "", native, usage, str(data.get("model") or model), choice.get("finish_reason")
    )


def _finalize(
    content: str, native: list[ToolCall], usage: Usage, model: str, finish: str | None
) -> ChatResult:
    if native:
        return ChatResult(strip_think(content), native, usage, model, finish)
    text, parsed = parse_tool_calls(content)
    return ChatResult(text, parsed, usage, model, "tool_calls" if parsed else finish)
