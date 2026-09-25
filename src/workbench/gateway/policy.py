"""Risk classification, deny-first policy and one-time approval tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

RiskLevel = Literal["read", "write", "destructive"]
LEVELS: tuple[RiskLevel, ...] = ("read", "write", "destructive")


@dataclass(frozen=True)
class PolicyConfig:
    verbs: dict[str, list[str]]
    unknown_default: RiskLevel = "write"
    require_approval: frozenset[str] = frozenset({"write", "destructive"})
    overrides: dict[str, RiskLevel] = field(default_factory=dict)
    rate_capacity: float = 10.0
    rate_refill_per_s: float = 2.0

    @classmethod
    def load(cls, path: Path) -> PolicyConfig:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rate = raw.get("rate_limit") or {}
        overrides = {str(k): _level(v) for k, v in (raw.get("overrides") or {}).items()}
        return cls(
            verbs={k: [str(v).lower() for v in vs] for k, vs in (raw.get("verbs") or {}).items()},
            unknown_default=_level(raw.get("unknown_default", "write")),
            require_approval=frozenset(
                _level(x) for x in raw.get("require_approval", ["write", "destructive"])
            ),
            overrides=overrides,
            rate_capacity=float(rate.get("capacity", 10.0)),
            rate_refill_per_s=float(rate.get("refill_per_s", 2.0)),
        )


def needs_approval_by_default(risk: str, config: PolicyConfig) -> bool:
    """Whether a call of this risk level needs approval when no approval rule matches: write and
    destructive always (ADR-030); read only if the tool policy's ``require_approval`` lists it."""
    return risk in ("write", "destructive") or risk in config.require_approval


def _level(value: Any) -> RiskLevel:
    if value not in LEVELS:
        raise ValueError(f"invalid risk level {value!r}; expected one of {LEVELS}")
    level: RiskLevel = value
    return level


@dataclass(frozen=True)
class Classification:
    tool: str
    level: RiskLevel
    source: Literal["override", "heuristic", "default", "http_method"]
    reason: str


def _tokens(name: str, description: str) -> list[str]:
    tokens = [t for t in name.lower().replace("-", "_").split("_") if t]
    first = description.strip().split(maxsplit=1)
    if first:
        tokens.append(first[0].lower().strip(".,:;"))
    return tokens


# Minimum risk implied by the route's HTTP method (ADR-015). GET and unknown methods add nothing.
METHOD_FLOOR: dict[str, RiskLevel] = {
    "POST": "write",
    "PUT": "write",
    "PATCH": "write",
    "DELETE": "destructive",
}


def _heuristic(tool: str, description: str, config: PolicyConfig) -> Classification:
    tokens = _tokens(tool, description)
    for level in ("destructive", "write", "read"):
        hits = [t for t in tokens if t in config.verbs.get(level, [])]
        if hits:
            return Classification(tool, _level(level), "heuristic", f"verb '{hits[0]}' => {level}")
    return Classification(tool, config.unknown_default, "default", "no known verb => unknown_default")


def classify(
    tool: str,
    description: str,
    config: PolicyConfig,
    scenario: str | None = None,
    http_method: str | None = None,
) -> Classification:
    """Override > (verb heuristic or unknown default, raised to the HTTP-method floor).

    ``http_method`` comes from the offline catalog (``envs.catalog.route_methods``); ``None``
    (tool not in the catalog) keeps the heuristic result unchanged. Overrides are reviewed
    human decisions and are applied as written (ADR-015).
    """
    for key in ([f"{scenario}__{tool}"] if scenario else []) + [tool]:
        if key in config.overrides:
            return Classification(tool, config.overrides[key], "override", f"override {key}")
    result = _heuristic(tool, description, config)
    method = (http_method or "").upper()
    floor = METHOD_FLOOR.get(method)
    if floor is not None and LEVELS.index(floor) > LEVELS.index(result.level):
        reason = f"{method} route => at least {floor} (was: {result.reason})"
        return Classification(tool, floor, "http_method", reason)
    return result


# --------------------------------------------------------------------------- approvals
class ApprovalError(PermissionError):
    """Missing, forged, expired, mismatched or already-used approval token."""


# The preview binding of a token approved without a successful preview (ADR-029)
PREVIEW_UNAVAILABLE = "preview_unavailable"
# Fields of a preview record covered by its signature (the digest covers the changes)
PREVIEW_SIGNED = ("id", "session_id", "tool", "args_digest", "status", "digest")


def args_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Grant:
    """What a redeemed token approved: who, and which preview (its digest or PREVIEW_UNAVAILABLE)."""

    approver: str
    preview: str = PREVIEW_UNAVAILABLE
    preview_id: str | None = None


class ApprovalService:
    """Issues HMAC-signed, single-use tokens bound to (session, tool, exact arguments, preview)."""

    def __init__(
        self, secret: bytes | None = None, ttl_s: float = 900.0, clock: Callable[[], float] = time.time
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._ttl = ttl_s
        self._clock = clock
        self._used: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env_var: str, ttl_s: float) -> ApprovalService:
        raw = os.environ.get(env_var)
        return cls(raw.encode() if raw else None, ttl_s)

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def issue(
        self,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        approver: str,
        *,
        preview: str = PREVIEW_UNAVAILABLE,
        preview_id: str | None = None,
    ) -> str:
        """``preview``: the digest of the preview the approver saw, or PREVIEW_UNAVAILABLE."""
        body = {
            "s": session_id,
            "t": tool,
            "a": args_digest(arguments),
            "by": approver,
            "exp": self._clock() + self._ttl,
            "n": secrets.token_hex(8),
            "p": preview,
            "pid": preview_id,
        }
        payload = json.dumps(body, sort_keys=True).encode()
        return base64.urlsafe_b64encode(payload).decode() + "." + self._sign(payload)

    def sign_record(self, record: dict[str, Any], fields: tuple[str, ...] = PREVIEW_SIGNED) -> str:
        """HMAC over ``fields`` of a record (a preview), so a copy kept elsewhere can be verified."""
        payload = json.dumps({k: record.get(k) for k in fields}, sort_keys=True, default=str).encode()
        return self._sign(payload)

    def verify_record(self, record: dict[str, Any], fields: tuple[str, ...] = PREVIEW_SIGNED) -> bool:
        sig = record.get("sig")
        return isinstance(sig, str) and hmac.compare_digest(self.sign_record(record, fields), sig)

    def consume(self, token: str, session_id: str, tool: str, arguments: dict[str, Any]) -> str:
        """Validate and burn a token. Returns the approver."""
        return self.redeem(token, session_id, tool, arguments).approver

    def redeem(self, token: str, session_id: str, tool: str, arguments: dict[str, Any]) -> Grant:
        """Validate and burn a token. Returns who approved it and the preview it is bound to."""
        try:
            b64, sig = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(b64.encode())
            body = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ApprovalError("malformed approval token") from exc
        if not hmac.compare_digest(self._sign(payload), sig):
            raise ApprovalError("approval token signature mismatch")
        if body["exp"] < self._clock():
            raise ApprovalError("approval token expired")
        if (body["s"], body["t"], body["a"]) != (session_id, tool, args_digest(arguments)):
            raise ApprovalError("approval token does not match this session/tool/arguments")
        with self._lock:
            if body["n"] in self._used:
                raise ApprovalError("approval token already used")
            self._used.add(body["n"])
        pid = body.get("pid")
        return Grant(str(body["by"]), str(body.get("p", PREVIEW_UNAVAILABLE)), str(pid) if pid else None)


# --------------------------------------------------------------------------- decisions
DecisionCode = Literal[
    "allowed", "not_allowlisted", "denied_by_rule", "approval_required", "approval_invalid", "rate_limited"
]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: DecisionCode
    risk: RiskLevel
    approver: str | None = None
    detail: str = ""
    grant: Grant | None = None  # set when an approval token was redeemed


class PolicyEngine:
    def __init__(self, config: PolicyConfig, approvals: ApprovalService) -> None:
        self.config = config
        self.approvals = approvals

    def requires_approval(self, risk: RiskLevel) -> bool:
        return risk in self.config.require_approval

    def decide(
        self,
        *,
        session_id: str,
        tool: str,
        risk: RiskLevel,
        allowlist: frozenset[str],
        arguments: dict[str, Any],
        approval_token: str | None,
        needs_approval: bool | None = None,
        denied: str | None = None,
    ) -> Decision:
        """Allowlist first, then the approval policy's answer when the gateway passes one
        (``denied``: the reason a rule refuses the call; ``needs_approval``: whether a token is
        required), else this tool policy's ``require_approval`` levels."""
        if tool not in allowlist:  # deny-first
            return Decision(
                False, "not_allowlisted", risk, detail=f"{tool} is not on this session's allowlist"
            )
        if denied is not None:
            return Decision(False, "denied_by_rule", risk, detail=denied)
        if not (self.requires_approval(risk) if needs_approval is None else needs_approval):
            return Decision(True, "allowed", risk)
        if not approval_token:
            return Decision(
                False, "approval_required", risk, detail=f"{risk} tool needs a one-time approval token"
            )
        try:
            grant = self.approvals.redeem(approval_token, session_id, tool, arguments)
        except ApprovalError as exc:
            return Decision(False, "approval_invalid", risk, detail=str(exc))
        return Decision(True, "allowed", risk, approver=grant.approver, grant=grant)
