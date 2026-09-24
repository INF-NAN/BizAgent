"""Gateway core: session routing, deny-first policy, rate limit, normalization and audit.

Tool names are exposed as ``<scenario>__<tool>`` so tools of different scenarios can never
collide. Every call — allowed, denied or failed — produces exactly one audit line.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from workbench.config import GatewaySettings
from workbench.gateway.audit import AuditLogger, AuditRecord, redact, summarize
from workbench.gateway.errors import GatewayError, NormalizedResult, normalize
from workbench.gateway.policy import ApprovalService, Classification, PolicyConfig, PolicyEngine, classify
from workbench.gateway.ratelimit import RateLimiter
from workbench.gateway.upstream import (
    McpUpstream,
    ToolSpec,
    Upstream,
    UpstreamTimeoutError,
    UpstreamTransportError,
)

SEP = "__"
CallStatus = Literal["ok", "empty", "error", "denied"]


class UnknownSessionError(KeyError):
    """No gateway session with this id."""


@dataclass
class GatewayTool:
    name: str  # prefixed
    tool: str  # upstream name
    description: str
    input_schema: dict[str, Any]
    classification: Classification

    @property
    def risk(self) -> str:
        return self.classification.level

    def as_dict(self, requires_approval: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk,
            "risk_source": self.classification.source,
            "risk_reason": self.classification.reason,
            "requires_approval": requires_approval,
        }


@dataclass
class SessionRoute:
    session_id: str
    scenario: str
    url: str
    allowlist: frozenset[str] = frozenset()
    tools: dict[str, GatewayTool] = field(default_factory=dict)


@dataclass
class CallOutcome:
    trace_id: str
    tool: str
    status: CallStatus
    text: str
    data: Any = None
    error: dict[str, Any] | None = None
    risk: str = "unknown"
    decision: str = "allowed"
    approver: str | None = None
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "tool": self.tool,
            "status": self.status,
            "text": self.text,
            "error": self.error,
            "risk": self.risk,
            "decision": self.decision,
            "approver": self.approver,
            "duration_ms": round(self.duration_ms, 1),
        }


def split_name(prefixed: str) -> tuple[str, str]:
    if SEP not in prefixed:
        raise ValueError(f"tool name must look like '<scenario>{SEP}<tool>', got {prefixed!r}")
    scenario, tool = prefixed.split(SEP, 1)
    return scenario, tool


class Gateway:
    def __init__(
        self,
        settings: GatewaySettings,
        *,
        policy: PolicyConfig | None = None,
        approvals: ApprovalService | None = None,
        upstream: Upstream | None = None,
        audit: AuditLogger | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_call: Callable[[CallOutcome], None] | None = None,
    ) -> None:
        self.settings = settings
        self.policy_config = policy or PolicyConfig.load(settings.policy_file)
        self.approvals = approvals or ApprovalService.from_env(
            settings.approval_secret_env, settings.approval_ttl_s
        )
        self.policy = PolicyEngine(self.policy_config, self.approvals)
        self.limiter = RateLimiter(
            self.policy_config.rate_capacity, self.policy_config.rate_refill_per_s, clock
        )
        self.upstream = upstream or McpUpstream()
        self.audit = audit or AuditLogger(settings.audit_path)
        self._routes: dict[str, SessionRoute] = {}
        self._on_call = on_call

    # ------------------------------------------------------------------ sessions
    async def register_session(
        self, session_id: str, scenario: str, url: str, allowlist: list[str] | None = None
    ) -> SessionRoute:
        """Discover tools once; deny-first: only tools named at registration are callable.

        ``allowlist=None`` means "every tool discovered right now" (explicitly enumerated,
        so tools that appear later are still denied); ``[]`` allows nothing.
        """
        specs = await self.upstream.list_tools(url, self.settings.upstream_timeout_s)
        route = SessionRoute(session_id, scenario, url)
        for spec in specs:
            route.tools[spec.name] = self._wrap(scenario, spec)
        names = set(route.tools)
        chosen = names if allowlist is None else {t for t in allowlist if t in names}
        route.allowlist = frozenset(chosen)
        self._routes[session_id] = route
        return route

    def unregister_session(self, session_id: str) -> None:
        self._routes.pop(session_id, None)

    def route(self, session_id: str) -> SessionRoute:
        if session_id not in self._routes:
            raise UnknownSessionError(session_id)
        return self._routes[session_id]

    def _wrap(self, scenario: str, spec: ToolSpec) -> GatewayTool:
        return GatewayTool(
            name=f"{scenario}{SEP}{spec.name}",
            tool=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            classification=classify(spec.name, spec.description, self.policy_config, scenario),
        )

    def list_tools(self, session_id: str) -> list[GatewayTool]:
        route = self.route(session_id)
        return [t for name, t in route.tools.items() if name in route.allowlist]

    def risk_table(self, session_id: str) -> list[dict[str, Any]]:
        route = self.route(session_id)
        return [
            {
                **t.as_dict(self.policy.requires_approval(t.classification.level)),
                "allowlisted": name in route.allowlist,
            }
            for name, t in sorted(route.tools.items())
        ]

    def requires_approval(self, session_id: str, prefixed: str) -> bool:
        _, tool = split_name(prefixed)
        gt = self.route(session_id).tools.get(tool)
        return gt is not None and self.policy.requires_approval(gt.classification.level)

    def issue_approval(self, session_id: str, prefixed: str, arguments: dict[str, Any], approver: str) -> str:
        self.route(session_id)
        _, tool = split_name(prefixed)
        return self.approvals.issue(session_id, tool, arguments, approver)

    # ------------------------------------------------------------------ calls
    async def call_tool(
        self,
        session_id: str,
        prefixed: str,
        arguments: dict[str, Any] | None = None,
        *,
        approval_token: str | None = None,
        trace_id: str | None = None,
    ) -> CallOutcome:
        args = dict(arguments or {})
        trace = trace_id or uuid.uuid4().hex
        started = time.perf_counter()
        route = self.route(session_id)
        outcome = await self._call(route, prefixed, args, approval_token, trace)
        outcome.duration_ms = (time.perf_counter() - started) * 1000
        self.audit.write(
            AuditRecord(
                trace_id=trace,
                session_id=session_id,
                tool=prefixed,
                risk=outcome.risk,
                decision=outcome.decision,
                approver=outcome.approver,
                args_summary=summarize(redact(args), self.settings.summary_max_chars),
                result_summary=summarize(outcome.error or outcome.text, self.settings.summary_max_chars),
                status=outcome.status,
                duration_ms=round(outcome.duration_ms, 1),
            )
        )
        if self._on_call:
            self._on_call(outcome)
        return outcome

    def _error(self, trace: str, prefixed: str, err: GatewayError, **kw: Any) -> CallOutcome:
        status: CallStatus = kw.pop("status", "error")
        return CallOutcome(trace, prefixed, status, json.dumps(err.as_dict()), error=err.as_dict(), **kw)

    async def _call(
        self, route: SessionRoute, prefixed: str, args: dict[str, Any], token: str | None, trace: str
    ) -> CallOutcome:
        try:
            scenario, tool = split_name(prefixed)
        except ValueError as exc:
            return self._error(
                trace, prefixed, GatewayError("unknown_tool", str(exc), self._available(route))
            )
        gt = route.tools.get(tool)
        if scenario != route.scenario or gt is None:
            err = GatewayError(
                "unknown_tool", f"{prefixed} is not a tool of this session", self._available(route)
            )
            return self._error(trace, prefixed, err, decision="unknown_tool")
        risk = gt.classification.level
        decision = self.policy.decide(
            session_id=route.session_id,
            tool=tool,
            risk=risk,
            allowlist=route.allowlist,
            arguments=args,
            approval_token=token,
        )
        if not decision.allowed:
            hints = (
                ["ask the user to approve this action first"] if decision.code == "approval_required" else []
            )
            err = GatewayError(f"policy_{decision.code}", decision.detail, hints)
            return self._error(trace, prefixed, err, status="denied", risk=risk, decision=decision.code)
        ok, retry_after = self.limiter.check(route.session_id, tool)
        if not ok:
            err = GatewayError(
                "rate_limited",
                f"too many calls to {tool}",
                [f"retry after {retry_after:.1f}s"],
                retryable=True,
            )
            return self._error(trace, prefixed, err, status="denied", risk=risk, decision="rate_limited")
        try:
            is_error, text = await self.upstream.call_tool(
                route.url, tool, args, self.settings.upstream_timeout_s
            )
        except UpstreamTimeoutError as exc:
            err = GatewayError("timeout", str(exc), ["the environment is slow; retry once"], retryable=True)
            return self._error(trace, prefixed, err, risk=risk, approver=decision.approver)
        except UpstreamTransportError as exc:
            err = GatewayError("transport_error", str(exc), ["the environment may be down"], retryable=True)
            return self._error(trace, prefixed, err, risk=risk, approver=decision.approver)
        norm: NormalizedResult = normalize(tool, is_error, text, gt.input_schema)
        return CallOutcome(
            trace,
            prefixed,
            norm.status,
            norm.text if norm.error is None else json.dumps(norm.error.as_dict()),
            data=norm.data,
            error=norm.error.as_dict() if norm.error else None,
            risk=risk,
            approver=decision.approver,
        )

    def _available(self, route: SessionRoute) -> list[str]:
        names = sorted(f"{route.scenario}{SEP}{t}" for t in route.allowlist)
        return [f"available tools: {names}"]
