"""Scenario catalog built from an AWM-format dataset directory (e.g. data/awm1k/).

Counts are derived offline from the dataset files (field layout: docs/RECON.md §1.6):
tool count = number of ``operation_id=`` route declarations in ``full_code`` (fastapi-mcp
names each tool after the route's operationId, see RECON §1.2); tasks from gen_tasks.jsonl;
tables from gen_db.jsonl ``db_schema.tables``. The official dataset is only read, never written.

``route_methods`` maps each tool to the HTTP method of its route (the gateway uses it as a
risk floor, ADR-015). It parses ``full_code`` with ``ast`` instead of executing it.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OPERATION_ID = re.compile(r"operation_id\s*=\s*['\"]([A-Za-z0-9_]+)['\"]")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
# Higher value wins when one operationId is declared on several routes (deny-first).
_METHOD_RANK = {"DELETE": 3, "POST": 2, "PUT": 2, "PATCH": 2}
_SCENARIO_PREFIX = re.compile(r'^\{\s*"scenario"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PATH_CONVERTER = re.compile(r"\{(\w+):[^}]*\}")


def normalize_scenario_name(name: str) -> str:
    # Same rule as AWM (third_party/agent-world-model/awm/tools.py:335-339), re-implemented so
    # the catalog does not import AWM's heavy dependency chain.
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return re.sub(r"_+", "_", s).strip("_").strip()


@dataclass(frozen=True)
class ScenarioInfo:
    name: str
    tools: int
    tasks: int
    tables: int
    tool_names: tuple[str, ...]


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def build_catalog(dataset_dir: Path) -> list[ScenarioInfo]:
    tasks = {
        normalize_scenario_name(r["scenario"]): len(r.get("tasks", []))
        for r in _iter_jsonl(dataset_dir / "gen_tasks.jsonl")
    }
    tables = {
        normalize_scenario_name(r["scenario"]): len((r.get("db_schema") or {}).get("tables", []))
        for r in _iter_jsonl(dataset_dir / "gen_db.jsonl")
    }
    out: list[ScenarioInfo] = []
    for r in _iter_jsonl(dataset_dir / "gen_envs.jsonl"):
        name = normalize_scenario_name(r["scenario"])
        tool_names = tuple(dict.fromkeys(OPERATION_ID.findall(r.get("full_code", ""))))
        out.append(
            ScenarioInfo(
                name=name,
                tools=len(tool_names),
                tasks=tasks.get(name, 0),
                tables=tables.get(name, 0),
                tool_names=tool_names,
            )
        )
    return sorted(out, key=lambda s: s.name)


def search(catalog: list[ScenarioInfo], keyword: str) -> list[ScenarioInfo]:
    kw = keyword.lower()
    return [s for s in catalog if kw in s.name or any(kw in t for t in s.tool_names)]


def save_index(catalog: list[ScenarioInfo], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(s) for s in catalog], indent=1), encoding="utf-8")


def load_tasks(dataset_dir: Path, scenario: str) -> list[str]:
    norm = normalize_scenario_name(scenario)
    for r in _iter_jsonl(dataset_dir / "gen_tasks.jsonl"):
        if normalize_scenario_name(r["scenario"]) == norm:
            return [str(t) for t in r.get("tasks", [])]
    return []


def _literal_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _default_operation_id(name: str, path: str, method: str) -> str:
    # FastAPI's generate_unique_id (fastapi 0.115.12 fastapi/utils.py:179-184): route name plus
    # path_format (converters dropped, starlette compile_path) with non-word chars -> "_", then
    # "_<method>". OpenAPI uses it when the route has no operation_id (fastapi/openapi/utils.py:237).
    path_format = _PATH_CONVERTER.sub(r"{\1}", path)
    return f"{re.sub(r'\W', '_', f'{name}{path_format}')}_{method}"


def route_methods(full_code: str) -> dict[str, str]:
    """Map operationId -> HTTP method (upper case) for every ``@<obj>.<method>(...)`` route.

    Unparseable code yields {} so callers fall back to the name heuristic. Routes whose path or
    operation_id is not a string literal are skipped. If one operationId is declared on several
    routes, the riskiest method wins.
    """
    try:
        tree = ast.parse(full_code)
    except SyntaxError:
        return {}
    methods: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            method = dec.func.attr.lower()
            if method not in HTTP_METHODS:
                continue
            kwargs = {k.arg: k.value for k in dec.keywords if k.arg}
            op_id = _literal_str(kwargs.get("operation_id"))
            if op_id is None:
                path = _literal_str(dec.args[0] if dec.args else kwargs.get("path"))
                if path is None:
                    continue
                op_id = _default_operation_id(_literal_str(kwargs.get("name")) or node.name, path, method)
            verb = method.upper()
            if _METHOD_RANK.get(verb, 0) >= _METHOD_RANK.get(methods.get(op_id, ""), 0):
                methods[op_id] = verb
    return methods


def _line_scenario(line: str) -> str | None:
    """Scenario name from the start of a gen_envs line without decoding the whole record."""
    m = _SCENARIO_PREFIX.match(line)
    return normalize_scenario_name(json.loads(f'"{m.group(1)}"')) if m else None


def load_route_methods(dataset_dir: Path, scenario: str) -> dict[str, str]:
    """Route methods of one scenario from ``gen_envs.jsonl``; {} if the scenario is absent.

    Like AWM's server (third_party/agent-world-model/awm/core/server.py:98-99), the last record
    of a scenario wins.
    """
    path = dataset_dir / "gen_envs.jsonl"
    if not path.exists():
        return {}
    target = normalize_scenario_name(scenario)
    code: str | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            name = _line_scenario(line)
            if name is not None and name != target:
                continue
            record = json.loads(line)
            if normalize_scenario_name(str(record.get("scenario", ""))) == target:
                code = str(record.get("full_code", ""))
    return route_methods(code) if code is not None else {}


def iter_route_methods(dataset_dir: Path) -> Iterator[tuple[str, dict[str, str]]]:
    """(scenario, route methods) for every record of ``gen_envs.jsonl``."""
    for r in _iter_jsonl(dataset_dir / "gen_envs.jsonl"):
        yield normalize_scenario_name(r["scenario"]), route_methods(r.get("full_code", ""))
