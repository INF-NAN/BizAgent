"""Token-bucket rate limiting per (session, tool)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self, capacity: float, refill_per_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = capacity
        self._last = clock()

    def try_take(self, cost: float = 1.0) -> bool:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_s)
        self._last = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after_s(self, cost: float = 1.0) -> float:
        missing = max(0.0, cost - self._tokens)
        return missing / self.refill_per_s if self.refill_per_s > 0 else float("inf")


class RateLimiter:
    def __init__(
        self, capacity: float, refill_per_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._capacity = capacity
        self._refill = refill_per_s
        self._clock = clock
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, session_id: str, tool: str) -> tuple[bool, float]:
        with self._lock:
            bucket = self._buckets.get((session_id, tool))
            if bucket is None:
                bucket = self._buckets[(session_id, tool)] = TokenBucket(
                    self._capacity, self._refill, self._clock
                )
            ok = bucket.try_take()
            return ok, 0.0 if ok else bucket.retry_after_s()
