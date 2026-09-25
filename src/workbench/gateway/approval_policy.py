"""Configurable approval policy (Phase 18, ADR-030): ordered rules decide, for each call, whether a
human must approve it, the policy approves it, or it is refused.

A rule matches on the tool name (globs), the scenario (globs), the risk level and conditions on
the arguments (numeric comparisons, enum membership). Rules are tried in file order and the first
match decides: ``auto_approve``, ``require_human`` or ``deny``. Without a match, write and
destructive calls need a human and read calls need nothing (``allow``).

Destructive calls are never auto-approved, whatever the file says: ``evaluate`` skips an
``auto_approve`` rule for a destructive call and keeps looking, and the gateway checks the risk
again before it approves anything on the policy's behalf. The loader also rejects a rule that
lists ``destructive`` next to ``auto_approve``.

Argument conditions are strict: a missing argument never matches, and a value that cannot be
compared (a string where a number is expected, a type the enum does not contain) makes a
``deny`` or ``require_human`` rule match and an ``auto_approve`` rule not match, so uncertainty
never loosens the decision.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from workbench.gateway.policy import RiskLevel

RuleDecision = Literal["auto_approve", "require_human", "deny"]
# "allow": no approval needed (read calls without a matching rule)
Outcome = Literal["allow", "auto_approve", "require_human", "deny"]
NUMERIC_OPS = ("lt", "lte", "gt", "gte", "eq", "ne")
SET_OPS = ("in", "not_in")
GUARD = "destructive calls are never auto-approved (ADR-030)"


class ApprovalPolicyError(ValueError):
    """The approval policy file does not match the schema; the message lists every problem."""


def _number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("must be a number (a string or a boolean is not)")
    return value


def _scalar(value: Any) -> Any:
    if isinstance(value, str | bool) or (isinstance(value, int | float) and math.isfinite(value)):
        return value
    raise ValueError("must be a string, a number or a boolean")


def _patterns(value: Any) -> Any:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(p, str) and p for p in items):
        raise ValueError("must be a non-empty glob or a non-empty list of globs")
    return items


Number = Annotated[int | float, BeforeValidator(_number)]
Scalar = Annotated[str | int | float | bool, BeforeValidator(_scalar)]
Globs = Annotated[list[str], BeforeValidator(_patterns)]


def _kind(value: Any) -> str | None:
    """Comparison kind of a scalar: booleans are not numbers here (True is not 1)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number" if math.isfinite(value) else None
    if isinstance(value, str):
        return "str"
    return None


def _member(value: Any, items: list[Any]) -> bool:
    return any(_kind(i) == _kind(value) and i == value for i in items)


def _compare(op: str, value: float, bound: float) -> bool:
    return {
        "lt": value < bound,
        "lte": value <= bound,
        "gt": value > bound,
        "gte": value >= bound,
        "eq": value == bound,
        "ne": value != bound,
    }[op]


class Condition(BaseModel):
    """Conditions on one argument; every operator given must hold."""

    model_config = ConfigDict(extra="forbid")

    lt: Number | None = None
    lte: Number | None = None
    gt: Number | None = None
    gte: Number | None = None
    eq: Number | None = None
    ne: Number | None = None
    in_: list[Scalar] | None = Field(default=None, alias="in", min_length=1)
    not_in: list[Scalar] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _has_operator(self) -> Condition:
        if not self.operators():
            raise ValueError(f"give at least one operator: {', '.join(NUMERIC_OPS + SET_OPS)}")
        return self

    def operators(self) -> dict[str, Any]:
        ops = {op: getattr(self, op) for op in NUMERIC_OPS if getattr(self, op) is not None}
        if self.in_ is not None:
            ops["in"] = self.in_
        if self.not_in is not None:
            ops["not_in"] = self.not_in
        return ops

    def test(self, value: Any) -> tuple[bool | None, str]:
        """(True, why) if every operator holds, (False, why) if one does not, (None, why) if the
        value cannot be compared (the caller decides strictly)."""
        for op, bound in self.operators().items():
            if op in NUMERIC_OPS:
                if _kind(value) != "number":
                    return None, f"is not a number, so '{op} {bound}' cannot be checked"
                if not _compare(op, value, bound):
                    return False, f"fails {op} {bound}"
                continue
            kinds = {_kind(i) for i in bound}
            if _kind(value) not in kinds:
                return None, f"cannot be compared with {op} {bound} (different type)"
            if (op == "in") != _member(value, bound):
                return False, f"fails {op} {bound}"
        return True, "holds"


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    description: str = ""
    tools: Globs = Field(default_factory=lambda: ["*"])  # globs over the tool name, e.g. "delete_*"
    scenarios: Globs = Field(default_factory=lambda: ["*"])  # globs over the scenario name
    risk: list[RiskLevel] | None = Field(default=None, min_length=1)  # None: any risk level
    args: dict[str, Condition] = Field(default_factory=dict)
    decision: RuleDecision

    @model_validator(mode="after")
    def _never_auto_approve_destructive(self) -> Rule:
        if self.decision == "auto_approve" and self.risk is not None and "destructive" in self.risk:
            raise ValueError(f"an auto_approve rule cannot list 'destructive' in risk: {GUARD}")
        return self

    def match(
        self, scenario: str, tool: str, risk: str, arguments: Mapping[str, Any]
    ) -> tuple[bool | None, str]:
        """(True|False, why), or (None, why) when an argument could not be compared."""
        if not any(fnmatchcase(tool, p) for p in self.tools):
            return False, f"tool {tool} does not match {self.tools}"
        if not any(fnmatchcase(scenario, p) for p in self.scenarios):
            return False, f"scenario {scenario} does not match {self.scenarios}"
        if self.risk is not None and risk not in self.risk:
            return False, f"risk {risk} is not in {self.risk}"
        unclear: list[str] = []
        for name, condition in self.args.items():
            if name not in arguments:
                return False, f"argument {name} is missing"
            ok, why = condition.test(arguments[name])
            if ok is False:
                return False, f"{name}={arguments[name]!r} {why}"
            if ok is None:
                unclear.append(f"{name}={arguments[name]!r} {why}")
        if unclear:
            return None, "; ".join(unclear)
        return True, "matches" + (f" ({', '.join(self.args)} checked)" if self.args else "")


@dataclass(frozen=True)
class RuleCheck:
    rule: str
    matched: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    """The policy's answer for one call.

    ``needs_token``: the call runs only with a one-time approval token — a human's for
    ``require_human``, and for ``auto_approve`` one the gateway issues on the policy's behalf
    (approver ``policy:<rule>``, bound to ``preview_unavailable``, owner decision D32).
    """

    decision: Outcome
    rule: str | None  # the deciding rule; None: the default applied
    reason: str
    risk: str
    needs_token: bool
    checks: tuple[RuleCheck, ...] = ()  # every rule tried, in order (for `gateway policy test`)
    guard: str | None = None  # set when the destructive guard skipped an auto_approve rule

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "rule": self.rule, "reason": self.reason, "guard": self.guard}


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> ApprovalPolicy:
        seen: set[str] = set()
        repeated: set[str] = set()
        for rule in self.rules:
            (repeated if rule.id in seen else seen).add(rule.id)
        if repeated:
            raise ValueError(f"rule ids must be unique; repeated: {sorted(repeated)}")
        return self

    @classmethod
    def empty(cls) -> ApprovalPolicy:
        return cls(version=1)

    @classmethod
    def load(cls, path: Path) -> ApprovalPolicy:
        """Load and validate; unknown fields and every other schema problem raise with their path."""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ApprovalPolicyError(f"{path}: cannot read the approval policy: {exc}") from exc
        try:
            return cls.model_validate(raw if raw is not None else {})
        except ValidationError as exc:
            problems = "\n".join(f"  - {_where(e['loc'], raw)}: {_message(e)}" for e in exc.errors())
            raise ApprovalPolicyError(f"{path}: invalid approval policy:\n{problems}") from exc

    def evaluate(
        self, *, scenario: str, tool: str, risk: str, arguments: Mapping[str, Any], needs_approval: bool
    ) -> Verdict:
        """First matching rule decides; ``needs_approval`` is the default for this risk level
        (write and destructive: True; read: False unless the tool policy says otherwise)."""
        checks: list[RuleCheck] = []
        guard: str | None = None
        for rule in self.rules:
            matched, why = rule.match(scenario, tool, risk, arguments)
            if matched is None:  # an argument could not be compared: never loosen the decision
                matched = rule.decision != "auto_approve"
                why += "; treated as a match" if matched else "; treated as no match"
            if matched and rule.decision == "auto_approve" and risk == "destructive":
                guard = f"rule {rule.id} skipped: {GUARD}"
                checks.append(RuleCheck(rule.id, False, f"{why}; skipped: {GUARD}"))
                continue
            checks.append(RuleCheck(rule.id, matched, why))
            if matched:
                label = f"rule {rule.id}" + (f": {rule.description}" if rule.description else "")
                # auto_approve only matters where approval is needed; for a read call it is "allowed"
                needs = rule.decision == "require_human" or (
                    rule.decision == "auto_approve" and needs_approval
                )
                return Verdict(rule.decision, rule.id, label, risk, needs, tuple(checks), guard)
        if needs_approval:
            reason = f"no rule matched: {risk} calls need a human by default"
            return Verdict("require_human", None, reason, risk, True, tuple(checks), guard)
        reason = f"no rule matched: {risk} calls need no approval"
        return Verdict("allow", None, reason, risk, False, tuple(checks), guard)


def _where(loc: tuple[int | str, ...], raw: Any) -> str:
    """rules[0] (id small-cart).args.quantity.lte — the YAML path of a validation error."""
    out = ""
    node = raw
    for i, part in enumerate(loc):
        if isinstance(part, int):
            out += f"[{part}]"
            node = node[part] if isinstance(node, list) and part < len(node) else None
            if i == 1 and loc[0] == "rules" and isinstance(node, dict) and isinstance(node.get("id"), str):
                out += f" (id {node['id']})"
        else:
            out += ("." if out else "") + str(part)
            node = node.get(part) if isinstance(node, dict) else None
    return out or "(file)"


def _message(error: Any) -> str:
    if error["type"] == "extra_forbidden":
        return "unknown field"
    msg = str(error["msg"])
    return msg.removeprefix("Value error, ")
