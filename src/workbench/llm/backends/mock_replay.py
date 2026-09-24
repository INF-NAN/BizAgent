"""Deterministic replay backend for CPU-only tests and the mock demo.

A fixture is a JSONL file under ``tests/fixtures/trajectories/``. The first line is a
metadata header (``{"_fixture": ...}``) that must state it is hand-written; every other line
is one assistant turn::

    {"content": "...", "tool_calls": [{"id": "c1", "name": "...", "arguments": {...}}],
     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

Turns are returned strictly in order; running past the end raises ``MockExhaustedError``
so that a test can never silently "succeed" on a missing turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workbench.llm.types import ChatResult, Message, ToolCall, Usage


class MockExhaustedError(RuntimeError):
    """The fixture has no more assistant turns."""


class FixtureFormatError(ValueError):
    """The fixture file is malformed."""


def load_fixture(path: Path) -> tuple[dict[str, Any], list[ChatResult]]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise FixtureFormatError(f"{path}: empty fixture")
    header = json.loads(lines[0])
    if "_fixture" not in header:
        raise FixtureFormatError(f"{path}: first line must be a {{'_fixture': ...}} header")
    turns: list[ChatResult] = []
    for i, line in enumerate(lines[1:], start=2):
        row = json.loads(line)
        calls = [
            ToolCall(
                id=str(c.get("id", f"call_{i}_{j}")), name=str(c["name"]), arguments=dict(c["arguments"])
            )
            for j, c in enumerate(row.get("tool_calls") or [])
        ]
        u = row.get("usage") or {}
        turns.append(
            ChatResult(
                content=str(row.get("content") or ""),
                tool_calls=calls,
                usage=Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))),
                model="mock_replay",
                finish_reason="tool_calls" if calls else "stop",
            )
        )
    return header, turns


class MockReplayBackend:
    name = "mock_replay"

    def __init__(
        self, fixture: Path | None = None, turns: list[ChatResult] | None = None, start_at: int = 0
    ) -> None:
        if turns is None:
            if fixture is None:
                raise ValueError("either fixture or turns is required")
            self.header, turns = load_fixture(fixture)
        else:
            self.header = {"_fixture": "in-memory"}
        self._turns = list(turns)
        self._cursor = start_at  # >0 simulates a process restart mid-conversation
        self.requests: list[list[Message]] = []

    @property
    def remaining(self) -> int:
        return len(self._turns) - self._cursor

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.requests.append(list(messages))
        if self._cursor >= len(self._turns):
            raise MockExhaustedError(f"mock fixture exhausted after {len(self._turns)} turns")
        turn = self._turns[self._cursor]
        self._cursor += 1
        return turn

    async def aclose(self) -> None:
        return None
