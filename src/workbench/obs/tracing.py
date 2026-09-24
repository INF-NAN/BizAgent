"""TraceHub: per-session event log (JSONL under the session's run dir) + live subscribers.

Events are operational records (node transitions, LLM token usage, gateway calls, approvals,
memory writes, terminations) — never model-quality metrics.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


class TraceHub:
    def __init__(self, runs_dir: Path | None) -> None:
        self.runs_dir = runs_dir
        self._memory: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = threading.Lock()

    def path(self, session_id: str) -> Path | None:
        return None if self.runs_dir is None else self.runs_dir / session_id / "trace.jsonl"

    def emit(self, session_id: str, type_: str, **data: Any) -> dict[str, Any]:
        event = {"ts": time.time(), "session_id": session_id, "type": type_, **data}
        with self._lock:
            self._memory[session_id].append(event)
            path = self.path(session_id)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            queues = list(self._subscribers.get(session_id, []))
        for q in queues:
            q.put_nowait(event)
        return event

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._subscribers[session_id].append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers.get(session_id, []):
                self._subscribers[session_id].remove(q)

    def events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.path(session_id)
        if path is not None and path.exists():
            return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        with self._lock:
            return list(self._memory.get(session_id, []))
