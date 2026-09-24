"""Prometheus operational metrics for the API.

These are OPERATIONS metrics (latency, call counts, errors, approval wait, active envs) —
not model-quality metrics, and must never be presented as such (rule R3).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.request_latency = Histogram(
            "workbench_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "workbench_tool_calls_total",
            "Gateway tool calls",
            ["status", "decision"],
            registry=self.registry,
        )
        self.errors = Counter("workbench_errors_total", "Errors by kind", ["kind"], registry=self.registry)
        self.approval_wait = Histogram(
            "workbench_approval_wait_seconds",
            "Time between an approval request and the human decision",
            buckets=(1, 5, 15, 30, 60, 120, 300, 900, 3600),
            registry=self.registry,
        )
        self.active_envs = Gauge(
            "workbench_active_envs", "Active environment sessions", registry=self.registry
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
