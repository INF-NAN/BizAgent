"""Long-term memory with a provenance gate, backed by LangGraph's SqliteStore.

Every fact carries a source tag: ``user_stated`` | ``tool_result`` | ``inferred``. Only the
first two may be written (model inferences are rejected and reported). Items expire after
``memory_ttl_s`` (LangGraph TTLConfig.default_ttl is in minutes: langgraph/store/base/__init__.py
TTLConfig; SqliteStore supports TTL: langgraph/store/sqlite/base.py:853).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langgraph.store.sqlite import SqliteStore

Source = Literal["user_stated", "tool_result", "inferred"]
ALLOWED_SOURCES: frozenset[str] = frozenset({"user_stated", "tool_result"})


class MemoryRejectedError(ValueError):
    """The fact did not pass the provenance gate."""


@dataclass(frozen=True)
class MemoryItem:
    key: str
    value: str
    source: str
    session_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "source": self.source, "session_id": self.session_id}


class MemoryService:
    def __init__(self, path: Path, ttl_s: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.store = SqliteStore(conn, ttl={"default_ttl": ttl_s / 60.0, "refresh_on_read": False})
        self.store.setup()

    @staticmethod
    def _ns(user_id: str) -> tuple[str, ...]:
        return ("memory", user_id)

    def put(
        self, user_id: str, key: str, value: str, source: str, session_id: str | None = None
    ) -> MemoryItem:
        if source not in ALLOWED_SOURCES:
            raise MemoryRejectedError(f"source {source!r} is not allowed (only {sorted(ALLOWED_SOURCES)})")
        if not key.strip() or not value.strip():
            raise MemoryRejectedError("empty key or value")
        item = MemoryItem(key.strip(), value.strip(), source, session_id)
        self.store.put(self._ns(user_id), item.key, item.as_dict())
        return item

    def list(self, user_id: str, limit: int = 100) -> list[MemoryItem]:
        self.store.sweep_ttl()
        return [
            MemoryItem(r.key, str(r.value["value"]), str(r.value["source"]), r.value.get("session_id"))
            for r in self.store.search(self._ns(user_id), limit=limit)
        ]

    def delete(self, user_id: str, key: str) -> bool:
        if self.store.get(self._ns(user_id), key) is None:
            return False
        self.store.delete(self._ns(user_id), key)
        return True
