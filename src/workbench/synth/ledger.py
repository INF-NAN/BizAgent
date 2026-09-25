"""Token / cost ledger for synthesis runs (estimates from configs/pricing.yaml)."""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LedgerEntry:
    step: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached: bool
    endpoint: str = "chat"
    # True when the proxy refused to forward the request (budget stop, ADR-023): nothing was billed.
    refused: bool = False
    # True when the request ended in an upstream error after the proxy's retries (ADR-024): an HTTP
    # error status, or a network error when ``upstream_status`` is None.
    failed: bool = False
    upstream_status: int | None = None
    error: str = ""
    # cache key of the request body; AWM retries a request with the same body, so this ties the
    # attempts of one request together (ADR-024). Empty in ledgers written before Phase 15.
    key: str = ""


@dataclass(frozen=True)
class Price:
    input_per_1m: float
    output_per_1m: float


def load_prices(path: Path) -> tuple[str, dict[str, Price]]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = {
        name: Price(float(p["input_per_1m"]), float(p["output_per_1m"]))
        for name, p in (raw.get("models") or {}).items()
    }
    return str(raw.get("currency", "USD")), models


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, entry: LedgerEntry) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry)) + "\n")

    def entries(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        return [
            LedgerEntry(**json.loads(ln))
            for ln in self.path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def spent(self, prices: dict[str, Price]) -> tuple[float, list[str]]:
        """Cost of the billed entries so far, and the models that have no price (ADR-023)."""
        total = 0.0
        unpriced: set[str] = set()
        for e in self.entries():
            if e.cached or e.refused or e.failed:
                continue
            price = prices.get(e.model)
            if price is None:
                unpriced.add(e.model)
                continue
            total += (
                e.prompt_tokens / 1e6 * price.input_per_1m + e.completion_tokens / 1e6 * price.output_per_1m
            )
        return total, sorted(unpriced)

    def summary(self, prices: dict[str, Price], currency: str = "USD") -> dict[str, Any]:
        steps: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "calls": 0,
                "cached_calls": 0,
                "refused_calls": 0,
                "failed_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
                "unpriced_models": set(),
            }
        )
        for e in self.entries():
            s = steps[e.step]
            if e.refused:  # never reached the upstream
                s["refused_calls"] += 1
                continue
            if e.failed:  # the upstream answered with an error, or could not be reached
                s["failed_calls"] += 1
                continue
            s["calls"] += 1
            if e.cached:
                s["cached_calls"] += 1
                continue  # cache hits cost nothing
            s["prompt_tokens"] += e.prompt_tokens
            s["completion_tokens"] += e.completion_tokens
            price = prices.get(e.model)
            if price is None:
                s["unpriced_models"].add(e.model)
            else:
                s["cost"] += (
                    e.prompt_tokens / 1e6 * price.input_per_1m
                    + e.completion_tokens / 1e6 * price.output_per_1m
                )
        out = {
            k: {**v, "unpriced_models": sorted(v["unpriced_models"]), "cost": round(v["cost"], 6)}
            for k, v in steps.items()
        }
        total = {
            "prompt_tokens": sum(v["prompt_tokens"] for v in out.values()),
            "completion_tokens": sum(v["completion_tokens"] for v in out.values()),
            "cost": round(sum(v["cost"] for v in out.values()), 6),
            "currency": currency,
            "complete": not any(v["unpriced_models"] for v in out.values()),
        }
        return {"steps": out, "total": total}


@dataclass(frozen=True)
class StepRequests:
    """How the LLM requests of one step ended (ADR-024).

    A request is identified by its cache key, so AWM's retries of it count once and the last
    attempt decides: a request that failed and then succeeded is not lost.
    """

    refused: int = 0  # ended refused by the budget stop (ADR-023)
    failed: int = 0  # ended in an upstream error after every retry
    failed_by_status: dict[str, int] = field(default_factory=dict)  # "402", "503", "network", ...


def step_requests(entries: list[LedgerEntry]) -> StepRequests:
    """Evaluate the ledger entries one step wrote (see StepRequests)."""
    last: dict[str, LedgerEntry] = {}
    for i, e in enumerate(entries):
        last[e.key or f"#{i}"] = e  # entries without a key (older ledgers) stand alone
    refused = [e for e in last.values() if e.refused]
    failed = [e for e in last.values() if e.failed]
    by_status = Counter("network" if e.upstream_status is None else str(e.upstream_status) for e in failed)
    return StepRequests(len(refused), len(failed), dict(sorted(by_status.items())))
