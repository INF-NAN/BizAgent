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


def _level(value: Any) -> RiskLevel:
    if value not in LEVELS:
        raise ValueError(f"invalid risk level {value!r}; expected one of {LEVELS}")
    level: RiskLevel = value
    return level


@dataclass(frozen=True)
class Classification:
    tool: str
    level: RiskLevel
    source: Literal["override", "heuristic", "default"]
    reason: str


def _tokens(name: str, description: str) -> list[str]:
    tokens = [t for t in name.lower().replace("-", "_").split("_") if t]
    first = description.strip().split(maxsplit=1)
    if first:
        tokens.append(first[0].lower().strip(".,:;"))
    return tokens


def classify(
    tool: str, description: str, config: PolicyConfig, scenario: str | None = None
) -> Classification:
    for key in ([f"{scenario}__{tool}"] if scenario else []) + [tool]:
        if key in config.overrides:
            return Classification(tool, config.overrides[key], "override", f"override {key}")
    tokens = _tokens(tool, description)
    for level in ("destructive", "write", "read"):
        hits = [t for t in tokens if t in config.verbs.get(level, [])]
        if hits:
            return Classification(tool, _level(level), "heuristic", f"verb '{hits[0]}' => {level}")
    return Classification(tool, config.unknown_default, "default", "no known verb => unknown_default")


# --------------------------------------------------------------------------- approvals
class ApprovalError(PermissionError):
    """Missing, forged, expired, mismatched or already-used approval token."""


def args_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalService:
    """Issues HMAC-signed, single-use tokens bound to (session, tool, exact arguments)."""

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

    def issue(self, session_id: str, tool: str, arguments: dict[str, Any], approver: str) -> str:
        body = {
            "s": session_id,
            "t": tool,
            "a": args_digest(arguments),
            "by": approver,
            "exp": self._clock() + self._ttl,
            "n": secrets.token_hex(8),
        }
        payload = json.dumps(body, sort_keys=True).encode()
        return base64.urlsafe_b64encode(payload).decode() + "." + self._sign(payload)

    def consume(self, token: str, session_id: str, tool: str, arguments: dict[str, Any]) -> str:
        """Validate and burn a token. Returns the approver."""
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
        return str(body["by"])


# --------------------------------------------------------------------------- decisions
DecisionCode = Literal["allowed", "not_allowlisted", "approval_required", "approval_invalid", "rate_limited"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: DecisionCode
    risk: RiskLevel
    approver: str | None = None
    detail: str = ""


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
    ) -> Decision:
        if tool not in allowlist:  # deny-first
            return Decision(
                False, "not_allowlisted", risk, detail=f"{tool} is not on this session's allowlist"
            )
        if not self.requires_approval(risk):
            return Decision(True, "allowed", risk)
        if not approval_token:
            return Decision(
                False, "approval_required", risk, detail=f"{risk} tool needs a one-time approval token"
            )
        try:
            approver = self.approvals.consume(approval_token, session_id, tool, arguments)
        except ApprovalError as exc:
            return Decision(False, "approval_invalid", risk, detail=str(exc))
        return Decision(True, "allowed", risk, approver=approver)
