"""Backend-neutral chat types shared by the LLM client, the agent and the mock backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str | None = None
    # Chain of thought returned next to content by thinking-mode servers (DeepSeek); the agent
    # does not use it, the LLM layer passes it back on later tool requests (ADR-017).
    reasoning_content: str | None = None


class ChatBackend(Protocol):
    """A backend performs exactly one chat completion attempt (retries live in the client)."""

    name: str

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    async def aclose(self) -> None: ...
