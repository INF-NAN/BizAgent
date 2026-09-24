"""Normalize upstream MCP results into ok / empty / error with actionable hints.

Upstream shapes (measured against the real AWM launcher, docs/RECON.md §10 Phase 2):
- ``Input validation error: 'x' is not one of ['a', 'b']``   (MCP SDK jsonschema check)
- ``Input validation error: 'field' is a required property``
- ``Error calling <tool>. Status code: <N>. Response: <body>`` (fastapi-mcp, server.py:558-561)
- a successful call whose text is ``[]`` / ``{}`` / ``null`` / empty -> EMPTY, never an error.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["ok", "empty", "error"]

VALIDATION = re.compile(r"^Input validation error: (?P<msg>.*)$", re.S)
NOT_ONE_OF = re.compile(r"^(?P<value>.+?) is not one of (?P<choices>\[.*\])")
REQUIRED = re.compile(r"^'(?P<field>[^']+)' is a required property")
HTTP = re.compile(
    r"^Error calling (?P<tool>\S+)\. Status code: (?P<code>\d{3})\. Response: (?P<body>.*)$", re.S
)


@dataclass
class GatewayError:
    code: str
    message: str
    hints: list[str] = field(default_factory=list)
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hints": self.hints,
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass
class NormalizedResult:
    status: Status
    text: str
    data: Any = None
    error: GatewayError | None = None


def _enum_hints(schema: dict[str, Any] | None) -> dict[str, list[Any]]:
    props = (schema or {}).get("properties", {})
    return {k: v["enum"] for k, v in props.items() if isinstance(v, dict) and "enum" in v}


def _required(schema: dict[str, Any] | None) -> list[str]:
    return list((schema or {}).get("required", []))


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _validation_error(msg: str, schema: dict[str, Any] | None) -> GatewayError:
    err = GatewayError(code="invalid_arguments", message=msg)
    if m := NOT_ONE_OF.match(msg):
        try:
            choices = ast.literal_eval(m.group("choices"))
        except (ValueError, SyntaxError):
            choices = m.group("choices")
        err.details["allowed_values"] = choices
        err.hints.append(f"value {m.group('value')} is not allowed; use one of {choices}")
    elif m := REQUIRED.match(msg):
        err.details["missing_fields"] = [m.group("field")]
        err.hints.append(f"add the required field '{m.group('field')}'")
    enums = _enum_hints(schema)
    if enums:
        err.details.setdefault("enums", enums)
    if _required(schema):
        err.hints.append(f"required fields: {_required(schema)}")
    return err


def _http_error(tool: str, status: int, body: str, schema: dict[str, Any] | None) -> GatewayError:
    parsed = _parse_json(body)
    if status == 422:
        err = GatewayError(code="invalid_arguments", message=f"{tool}: request rejected (422)")
        for item in (parsed or {}).get("detail", []) if isinstance(parsed, dict) else []:
            loc = ".".join(str(x) for x in item.get("loc", []) if x not in ("body", "query", "path"))
            if item.get("type") == "missing":
                err.details.setdefault("missing_fields", []).append(loc)
                err.hints.append(f"add the required field '{loc}'")
            elif "expected" in (item.get("ctx") or {}):
                err.hints.append(f"'{loc}' must be one of {item['ctx']['expected']}")
            else:
                err.hints.append(f"'{loc}': {item.get('msg')}")
        return err
    if status == 404:
        return GatewayError(
            "not_found", f"{tool}: resource not found (404)", ["check the id with a list/search tool first"]
        )
    if status >= 500:
        return GatewayError(
            "upstream_server_error",
            f"{tool}: environment returned HTTP {status}",
            ["the environment could not process this call; check ids/arguments or try a different tool"],
            retryable=False,
            details={"body": body[:500]},
        )
    return GatewayError("upstream_http_error", f"{tool}: HTTP {status}", [], details={"body": body[:500]})


def is_empty_payload(text: str) -> bool:
    stripped = text.strip()
    if stripped in ("", "null", '""'):
        return True
    parsed = _parse_json(stripped)
    return bool(parsed == [] or parsed == {})


def normalize(tool: str, is_error: bool, text: str, schema: dict[str, Any] | None = None) -> NormalizedResult:
    if not is_error:
        if is_empty_payload(text):
            return NormalizedResult("empty", text, data=_parse_json(text))
        return NormalizedResult("ok", text, data=_parse_json(text))
    if m := VALIDATION.match(text):
        return NormalizedResult("error", text, error=_validation_error(m.group("msg"), schema))
    if m := HTTP.match(text):
        return NormalizedResult(
            "error", text, error=_http_error(tool, int(m.group("code")), m.group("body"), schema)
        )
    if text.startswith("Unknown tool"):
        return NormalizedResult(
            "error", text, error=GatewayError("unknown_tool", text, ["call list_tools first"])
        )
    return NormalizedResult(
        "error", text, error=GatewayError("upstream_error", text or "unknown upstream error")
    )
