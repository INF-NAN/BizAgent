"""Token / cost ledger for synthesis runs (estimates from configs/pricing.yaml)."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
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
            if e.cached or e.refused:
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
