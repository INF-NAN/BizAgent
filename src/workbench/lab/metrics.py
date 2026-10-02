"""Statistics used by the reports: Wilson intervals, pass@k, paired tests, failure categories."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from typing import Any

GUARD_REASONS = ("max_steps", "repeated_call", "no_state_change", "token_budget", "wall_clock")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k from n samples with c successes (Chen et al., 2021)."""
    if k > n:
        raise ValueError(f"k={k} > n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts b (A only) and c (B only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def paired_bootstrap(
    a: Sequence[int], b: Sequence[int], iters: int = 5000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of b - a with a 95% bootstrap interval over paired items."""
    if len(a) != len(b) or not a:
        raise ValueError("paired samples must be non-empty and the same length")
    diffs = [y - x for x, y in zip(a, b, strict=True)]
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return (sum(diffs) / n, means[int(0.025 * iters)], means[int(0.975 * iters) - 1])


def failure_category(r: dict[str, Any]) -> str:
    """One label per episode, checked in order; 'success' when the verifier says complete."""
    if r.get("status") != "ok":
        return f"error:{r.get('status')}"
    if r.get("success"):
        return "success"
    reason = (r.get("termination") or {}).get("reason")
    if reason == "llm_error":
        return "llm_error"
    if reason == "plan_invalid":
        return "plan_invalid"
    if reason in GUARD_REASONS:
        return f"guard:{reason}"
    if r.get("tool_calls", 0) == 0:
        return "no_tool_call"
    if r.get("tool_errors", 0) > 0:
        return "tool_errors"
    if r.get("writes", 0) == 0:
        return "no_write"
    return "wrong_outcome"


def rate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    n = len(rows)
    k = sum(1 for r in rows if r.get("success"))
    lo, hi = wilson(k, n)
    return {"n": n, "success": k, "rate": (k / n) if n else 0.0, "ci95": [lo, hi]}
