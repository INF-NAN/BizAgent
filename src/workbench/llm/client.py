"""Unified LLM client: backend selection by config, retries, wall-clock cap, token accounting.

Switching backends is configuration only (``llm.backend`` = mock_replay | vllm | openai_compat).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from workbench.config import LLMSettings
from workbench.llm.backends.mock_replay import MockReplayBackend
from workbench.llm.backends.openai_compat import OpenAICompatBackend
from workbench.llm.errors import LLMRetryableError, LLMTimeoutError
from workbench.llm.types import ChatBackend, ChatResult, Message, Usage

UsageHook = Callable[[dict[str, Any]], None]

# AWM's own agent sends this to vLLM (third_party/agent-world-model/awm/core/agent.py:374-379).
VLLM_EXTRA_BODY: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": True}}


class LLMClient:
    def __init__(
        self,
        backend: ChatBackend,
        settings: LLMSettings,
        *,
        on_usage: UsageHook | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.usage = Usage()
        self.calls = 0
        self._on_usage = on_usage
        self._sleep = sleep
        self._rand = rand

    def backoff_s(self, attempt: int) -> float:
        base = float(min(self.settings.backoff_max_s, self.settings.backoff_base_s * (2**attempt)))
        return base * (0.5 + self._rand() / 2)  # jitter in [0.5, 1.0) x base

    async def chat(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, *, purpose: str = "chat"
    ) -> ChatResult:
        s = self.settings
        started = time.perf_counter()
        attempts = 0
        try:
            async with asyncio.timeout(s.total_timeout_s):  # layer 2: overall wall clock
                while True:
                    attempts += 1
                    try:
                        result = await self.backend.chat(
                            messages, tools, temperature=s.temperature, max_tokens=s.max_tokens
                        )
                        break
                    except LLMRetryableError:
                        if attempts > s.max_retries:
                            raise
                        await self._sleep(self.backoff_s(attempts - 1))
        except TimeoutError as exc:
            raise LLMTimeoutError(f"chat exceeded wall-clock budget of {s.total_timeout_s}s") from exc
        self.calls += 1
        self.usage.add(result.usage)
        if self._on_usage:
            self._on_usage(
                {
                    "purpose": purpose,
                    "backend": self.backend.name,
                    "model": result.model,
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "attempts": attempts,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        return result

    async def aclose(self) -> None:
        await self.backend.aclose()


def build_backend(settings: LLMSettings) -> ChatBackend:
    if settings.backend == "mock_replay":
        return MockReplayBackend(settings.mock_fixture)
    return OpenAICompatBackend(
        name=settings.backend,
        base_url=settings.base_url,
        model=settings.model,
        api_key=os.environ.get(settings.api_key_env),
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        stream=settings.stream,
        extra_body=VLLM_EXTRA_BODY if settings.backend == "vllm" else None,
    )


def build_llm_client(settings: LLMSettings, on_usage: UsageHook | None = None) -> LLMClient:
    return LLMClient(build_backend(settings), settings, on_usage=on_usage)
