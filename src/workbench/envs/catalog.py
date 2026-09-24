"""Scenario catalog built from an AWM-format dataset directory (e.g. data/awm1k/).

Counts are derived offline from the dataset files (field layout: docs/RECON.md §1.6):
tool count = number of ``operation_id=`` route declarations in ``full_code`` (fastapi-mcp
names each tool after the route's operationId, see RECON §1.2); tasks from gen_tasks.jsonl;
tables from gen_db.jsonl ``db_schema.tables``. The official dataset is only read, never written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OPERATION_ID = re.compile(r"operation_id\s*=\s*['\"]([A-Za-z0-9_]+)['\"]")


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
