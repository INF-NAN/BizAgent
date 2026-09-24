"""Pass ``reasoning_content`` back on later requests that carry ``tools`` (ADR-017).

DeepSeek thinking mode (https://api-docs.deepseek.com/guides/thinking_mode, read 2026-09-24):

- the chain of thought is returned as ``reasoning_content`` next to ``content``
  (streaming: ``delta.reasoning_content``);
- if a request carries ``tools``, the ``reasoning_content`` of all previous assistant turns must
  be passed back, also for turns without a tool call, otherwise the API returns HTTP 400;
- if a request carries no ``tools``, it need not be passed back and is ignored.

Callers such as the agent keep only ``content`` and ``tool_calls`` in their history, so the LLM
layer remembers the reasoning of each assistant turn it produced and re-attaches it when that
turn appears again in a request with ``tools``. A turn is identified by the non-system messages
before it plus its own content and first tool-call id: re-planning rebuilds the system prompt
but keeps the rest of the history.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from workbench.llm.types import ChatResult, Message

DEFAULT_MAX_ENTRIES = 1024


def _canonical(message: Message) -> list[Any] | None:
    """Stable identity of one message; ``None`` for system messages."""
    role = message.get("role")
    if role == "system":
        return None
    if role == "assistant":
        calls = message.get("tool_calls") or []
        first = calls[0].get("id") if calls and isinstance(calls[0], dict) else None
        return ["assistant", message.get("content") or "", first]
    if role == "tool":
        return ["tool", message.get("tool_call_id"), message.get("content")]
    return [str(role), message.get("content")]


def _digest(parts: list[list[Any]]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class ReasoningStore:
    """Bounded LRU map: assistant turn identity -> the reasoning_content it was generated with."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, request: list[Message], result: ChatResult) -> None:
        """Record the reasoning of the reply to ``request`` (as the caller will store the reply)."""
        if not result.reasoning_content:
            return
        reply: Message = {"role": "assistant", "content": result.content}
        if result.tool_calls:
            reply["tool_calls"] = [{"id": result.tool_calls[0].id}]
        key = _digest([c for c in map(_canonical, [*request, reply]) if c is not None])
        self._entries[key] = result.reasoning_content
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def attach(self, messages: list[Message]) -> list[Message]:
        """Copy of ``messages`` with the known reasoning_content added to assistant turns.

        Turns that already carry ``reasoning_content`` are left as they are; turns this store
        never saw (another backend, a restarted process, evicted entries) are sent without it.
        The caller's message dicts are not modified.
        """
        out: list[Message] = []
        prefix: list[list[Any]] = []
        for message in messages:
            canon = _canonical(message)
            if canon is None:
                out.append(message)
                continue
            prefix.append(canon)
            if message.get("role") == "assistant" and "reasoning_content" not in message:
                key = _digest(prefix)
                reasoning = self._entries.get(key)
                if reasoning is not None:
                    self._entries.move_to_end(key)
                    message = {**message, "reasoning_content": reasoning}
            out.append(message)
        return out
