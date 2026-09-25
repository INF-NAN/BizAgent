"""Gateway core: session routing, deny-first policy, rate limit, normalization and audit.

Tool names are exposed as ``<scenario>__<tool>`` so tools of different scenarios can never
collide. Every call — allowed, denied or failed — produces exactly one audit line.

Approval previews (ADR-029): ``preview`` runs a write/destructive call in a shadow environment
(through the env service) and returns a signed record of the rows it would change;
``issue_approval`` binds the token to that preview's digest, or to ``preview_unavailable`` when
the risk level allows approving without one (``approval.require_preview``); an approved call is
measured (checkpoint before, changes after) and compared with its preview in the audit line.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from workbench.config import ApprovalSettings, GatewaySettings
from workbench.envs.changes import compare
from workbench.envs.changes import digest as changes_digest
from workbench.envs.changes import summary as changes_summary
from workbench.gateway.audit import AuditLogger, AuditRecord, redact, summarize
from workbench.gateway.errors import GatewayError, NormalizedResult, normalize
from workbench.gateway.policy import (
    PREVIEW_UNAVAILABLE,
    ApprovalError,
    ApprovalService,
    Classification,
    Decision,
    Grant,
    PolicyConfig,
    PolicyEngine,
    args_digest,
    classify,
)
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
PREVIEWS_KEPT = 256  # preview records kept in memory for binding and comparison


class PreviewBackend(Protocol):
    """Runs previews and measures real calls; the env service implements it (ADR-029)."""

    async def preview(
        self, session_id: str, tool: str, arguments: dict[str, Any], timeout_s: float, max_rows: int = 20
    ) -> dict[str, Any]: ...
    async def checkpoint(self, session_id: str) -> str: ...
    async def changes_since(self, session_id: str, checkpoint: str) -> dict[str, Any]: ...


class UnknownSessionError(KeyError):
    """No gateway session with this id."""


@dataclass
class GatewayTool:
    name: str  # prefixed
    tool: str  # upstream name
    description: str
    input_schema: dict[str, Any]
    classification: Classification
    http_method: str | None = None  # from the offline catalog; None if unknown

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
            "http_method": self.http_method,
            "requires_approval": requires_approval,
        }


@dataclass
class SessionRoute:
    session_id: str
    scenario: str
    url: str
    allowlist: frozenset[str] = frozenset()
    tools: dict[str, GatewayTool] = field(default_factory=dict)
    tool_methods: dict[str, str] = field(default_factory=dict)


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
    # approved calls only: the preview binding and how the real changes compared with it
    preview: dict[str, Any] | None = None
    preview_check: dict[str, Any] | None = None

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
            "preview": self.preview,
            "preview_check": self.preview_check,
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
        approval: ApprovalSettings | None = None,
        previews: PreviewBackend | None = None,
    ) -> None:
        self.settings = settings
        self.approval_settings = approval or ApprovalSettings()
        self.previews = previews
        self._preview_records: OrderedDict[str, dict[str, Any]] = OrderedDict()
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
        self,
        session_id: str,
        scenario: str,
        url: str,
        allowlist: list[str] | None = None,
        tool_methods: dict[str, str] | None = None,
    ) -> SessionRoute:
        """Discover tools once; deny-first: only tools named at registration are callable.

        ``allowlist=None`` means "every tool discovered right now" (explicitly enumerated,
        so tools that appear later are still denied); ``[]`` allows nothing.
        ``tool_methods`` (tool -> HTTP method, from the offline catalog) sets a risk floor;
        tools missing from it keep the name heuristic (ADR-015).
        """
        specs = await self.upstream.list_tools(url, self.settings.upstream_timeout_s)
        route = SessionRoute(session_id, scenario, url, tool_methods=dict(tool_methods or {}))
        for spec in specs:
            route.tools[spec.name] = self._wrap(scenario, spec, route.tool_methods.get(spec.name))
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

    def _wrap(self, scenario: str, spec: ToolSpec, http_method: str | None = None) -> GatewayTool:
        return GatewayTool(
            name=f"{scenario}{SEP}{spec.name}",
            tool=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            classification=classify(
                spec.name, spec.description, self.policy_config, scenario, http_method=http_method
            ),
            http_method=http_method,
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

    # ------------------------------------------------------------------ approval previews
    def attach_previews(self, backend: PreviewBackend) -> None:
        self.previews = backend

    def preview_required(self, risk: str) -> bool:
        return any(level == risk and on for level, on in self.approval_settings.require_preview.items())

    async def preview(self, session_id: str, prefixed: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run the call on a shadow copy of the session's current DB and describe what it changes.

        Returns a signed, JSON-safe record: ``status`` ok | failed | unavailable (no env service),
        ``changes`` and their ``digest``, ``error`` and ``stage`` on failure, ``required`` (this
        risk level needs a successful preview), ``approvable`` and a one-line ``summary``.
        """
        route = self.route(session_id)
        scenario, tool = split_name(prefixed)
        gt = route.tools.get(tool)
        if scenario != route.scenario or gt is None:
            raise ValueError(f"{prefixed} is not a tool of this session")
        args = dict(arguments)
        risk = gt.classification.level
        s = self.approval_settings
        started = time.perf_counter()
        result: dict[str, Any]
        if self.previews is None:
            result = {"status": "unavailable", "error": "this gateway has no env-manager to run previews"}
        else:
            try:
                result = await asyncio.wait_for(
                    self.previews.preview(session_id, tool, args, s.preview_timeout_s, s.preview_max_rows),
                    s.preview_timeout_s + 30,  # the env-manager enforces the timeout; this is a backstop
                )
            except Exception as exc:  # env-manager unreachable, HTTP error, ...
                result = {"status": "failed", "stage": "env-manager", "error": f"{type(exc).__name__}: {exc}"}
        ok = result.get("status") == "ok"
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "stage": None,
            "call": None,
            "changes": None,
            "digest": None,
            "timings_ms": {},
            **result,
            "session_id": session_id,
            "tool": prefixed,
            "risk": risk,
            "args_digest": args_digest(args),
            "required": self.preview_required(risk),
        }
        record["error"] = None if ok else str(record.get("error") or record["status"])[:1000]
        record["approvable"] = ok or not record["required"]
        record["summary"] = changes_summary(record["changes"]) if ok else record["error"]
        record["sig"] = self.approvals.sign_record(record)
        self._remember(record)
        self.audit.write(
            AuditRecord(
                trace_id=record["id"],
                session_id=session_id,
                tool=prefixed,
                risk=risk,
                decision="preview",
                approver=None,
                args_summary=summarize(redact(args), self.settings.summary_max_chars),
                result_summary=summarize(record["summary"], self.settings.summary_max_chars),
                status=str(record["status"]),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                preview={k: record[k] for k in ("id", "status", "digest", "stage", "required")},
            )
        )
        return record

    def _remember(self, record: dict[str, Any]) -> None:
        self._preview_records[str(record["id"])] = record
        self._preview_records.move_to_end(str(record["id"]))
        while len(self._preview_records) > PREVIEWS_KEPT:
            self._preview_records.popitem(last=False)

    def _verified(
        self, session_id: str, prefixed: str, arguments: dict[str, Any], record: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """``record`` if this gateway signed it for exactly this call, else None."""
        if record is None or not self.approvals.verify_record(record):
            return None
        if (record.get("session_id"), record.get("tool"), record.get("args_digest")) != (
            session_id,
            prefixed,
            args_digest(arguments),
        ):
            return None
        if record.get("status") == "ok" and changes_digest(record.get("changes")) != record.get("digest"):
            return None
        return record

    def issue_approval(
        self,
        session_id: str,
        prefixed: str,
        arguments: dict[str, Any],
        approver: str,
        *,
        preview: dict[str, Any] | None = None,
        preview_id: str | None = None,
    ) -> str:
        """Issue a one-time token bound to (session, tool, arguments, preview).

        ``preview`` is the record the approver saw (or ``preview_id`` names one this gateway keeps).
        Without a verified, successful preview the token is bound to ``preview_unavailable`` —
        unless ``approval.require_preview`` is on for the tool's risk level: then no token is
        issued and ApprovalError says why (only a rejection is possible).
        """
        route = self.route(session_id)
        _, tool = split_name(prefixed)
        gt = route.tools.get(tool)
        risk = gt.classification.level if gt is not None else self.policy_config.unknown_default
        if preview is None and preview_id is not None:
            preview = self._preview_records.get(preview_id)
        record = self._verified(session_id, prefixed, arguments, preview)
        if record is not None and record.get("status") == "ok":
            self._remember(record)  # a checkpointed copy after a restart: keep it for the comparison
            return self.approvals.issue(
                session_id,
                tool,
                arguments,
                approver,
                preview=str(record["digest"]),
                preview_id=str(record["id"]),
            )
        if self.preview_required(risk):
            if preview is None:
                why = "no preview was run"
            elif record is None:
                why = "the preview could not be verified for this call"
            else:
                why = f"the preview failed: {record.get('error')}"
            raise ApprovalError(
                f"approving a {risk} call needs a successful preview ({why}); only rejection is possible"
            )
        return self.approvals.issue(session_id, tool, arguments, approver)

    async def _checkpoint(self, session_id: str) -> tuple[str | None, dict[str, Any] | None]:
        if self.previews is None:
            return None, None
        try:
            return await self.previews.checkpoint(session_id), None
        except Exception as exc:
            return None, {
                "result": "check_failed",
                "reason": f"checkpoint failed: {type(exc).__name__}: {exc}",
            }

    async def _check_preview(
        self, session_id: str, grant: Grant, checkpoint: str | None, failed: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Compare what the approved call changed with the preview its token is bound to."""
        if failed is not None:
            return failed
        actual: dict[str, Any] | None = None
        if checkpoint is not None and self.previews is not None:
            try:
                actual = await self.previews.changes_since(session_id, checkpoint)
            except Exception as exc:
                return {
                    "result": "check_failed",
                    "reason": f"measuring the call failed: {type(exc).__name__}: {exc}",
                }
        measured = changes_summary(actual) if actual is not None else None
        if grant.preview == PREVIEW_UNAVAILABLE:
            return {
                "result": "preview_unavailable",
                "reason": "approved without a successful preview",
                "actual": measured,
            }
        record = self._preview_records.get(grant.preview_id or "")
        if record is None or record.get("digest") != grant.preview:
            return {
                "result": "check_failed",
                "reason": "the approved preview is not known to this gateway",
                "actual": measured,
            }
        if actual is None:
            return {
                "result": "check_failed",
                "reason": "no env-manager to measure the call",
                "preview": record.get("summary"),
            }
        return {**compare(record["changes"], actual), "preview": record.get("summary"), "actual": measured}

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
                preview=outcome.preview,
                preview_check=redact(outcome.preview_check),
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
        grant = decision.grant
        if grant is None:
            return await self._upstream(route, prefixed, tool, gt, args, decision, trace)
        # an approved call: measure what it changes and compare it with the approved preview
        checkpoint, failed = await self._checkpoint(route.session_id)
        outcome = await self._upstream(route, prefixed, tool, gt, args, decision, trace)
        outcome.preview = {"binding": grant.preview, "id": grant.preview_id}
        outcome.preview_check = await self._check_preview(route.session_id, grant, checkpoint, failed)
        return outcome

    async def _upstream(
        self,
        route: SessionRoute,
        prefixed: str,
        tool: str,
        gt: GatewayTool,
        args: dict[str, Any],
        decision: Decision,
        trace: str,
    ) -> CallOutcome:
        risk = gt.classification.level
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
        norm: NormalizedResult = normalize(tool, is_error, text, gt.input_schema, read_only=risk == "read")
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
