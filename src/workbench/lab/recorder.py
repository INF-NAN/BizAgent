"""Record every chat request and reply per episode (the raw material for SFT and serving replays).

The recording backend wraps the real one; a context variable names the running episode, so
concurrent episodes sharing one LLM client keep separate recordings.
"""

from __future__ import annotations

import contextvars
import copy
import gzip
import json
import time
from pathlib import Path
from typing import Any

from workbench.llm.types import ChatBackend, ChatResult, Message

CURRENT_EPISODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("lab_episode", default=None)


class RecordingBackend:
    def __init__(self, inner: ChatBackend) -> None:
        self.inner = inner
        self.name = inner.name
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self.tools: dict[str, list[dict[str, Any]]] = {}

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        started = time.perf_counter()
        result = await self.inner.chat(messages, tools, temperature=temperature, max_tokens=max_tokens)
        episode = CURRENT_EPISODE.get()
        if episode is not None:
            if tools and episode not in self.tools:
                self.tools[episode] = copy.deepcopy(tools)
            self.calls.setdefault(episode, []).append(
                {
                    "messages": copy.deepcopy(messages),
                    "with_tools": bool(tools),
                    "response": {
                        "content": result.content,
                        "tool_calls": [
                            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls
                        ],
                        "finish_reason": result.finish_reason,
                    },
                    "usage": {
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                    },
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        return result

    def pop(self, episode: str) -> dict[str, Any]:
        return {"tools": self.tools.pop(episode, []), "calls": self.calls.pop(episode, [])}

    async def aclose(self) -> None:
        await self.inner.aclose()


def save_recording(path: Path, recording: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(recording, f, ensure_ascii=False)


def load_recording(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data
