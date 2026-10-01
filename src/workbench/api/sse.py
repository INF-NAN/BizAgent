"""Server-Sent Events helpers (sse-starlette 3.0.3: sse_starlette/sse.py EventSourceResponse).

On client disconnect sse-starlette cancels the streaming task group
(sse_starlette/sse.py:194-202, 262-275); the cancellation propagates into the agent turn,
which records a ``cancelled`` trace event (workbench.agent.runner).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from sse_starlette.sse import EventSourceResponse


def to_sse(
    events: AsyncIterator[dict[str, Any]], on_event: Callable[[dict[str, Any]], None] | None = None
) -> EventSourceResponse:
    async def gen() -> AsyncIterator[dict[str, str]]:
        async for event in events:
            if on_event:
                on_event(event)
            yield {
                "event": str(event.get("type", "message")),
                "data": json.dumps(event, ensure_ascii=False, default=str),
            }

    return EventSourceResponse(gen(), ping=15)
