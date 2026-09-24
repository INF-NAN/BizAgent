"""Post-synthesis validation report built from `awm env check_all` output.

`check_all` (third_party/agent-world-model/awm/core/test_env.py:21-53) logs one
``PASSED: <scenario>`` / ``FAILED: <scenario>`` line per environment with an error preview.
We parse those lines, count tools from gen_envs.jsonl (catalog) and bucket failures by cause.
The counts are engineering facts about THIS run's generated code, not model metrics.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench.envs.catalog import build_catalog

PASSED = re.compile(r"PASSED: (\S+)")
FAILED = re.compile(r"FAILED: (\S+)\s*\n\s*(.*)")

CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("syntax_error", re.compile(r"SyntaxError|IndentationError")),
    ("import_error", re.compile(r"ImportError|ModuleNotFoundError")),
    (
        "sqlalchemy_error",
        re.compile(r"sqlalchemy|ArgumentError|AmbiguousForeignKeys|NoForeignKeysError", re.I),
    ),
    ("pydantic_error", re.compile(r"pydantic|ValidationError|PydanticUserError", re.I)),
    ("fastapi_error", re.compile(r"FastAPIError|fastapi", re.I)),
    ("timeout", re.compile(r"timeout|timed out", re.I)),
    ("database_missing", re.compile(r"No such file|not found", re.I)),
]


def categorize(preview: str) -> str:
    for name, pattern in CATEGORIES:
        if pattern.search(preview):
            return name
    return "other"


@dataclass
class ValidationReport:
    total: int
    started: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    failure_categories: dict[str, int] = field(default_factory=dict)
    tools_per_env: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "environments_total": self.total,
            "environments_started": len(self.started),
            "environments_failed": len(self.failed),
            "tools_total": sum(self.tools_per_env.values()),
            "tools_per_env": self.tools_per_env,
            "failure_categories": self.failure_categories,
            "failed": self.failed,
        }

    def markdown(self) -> str:
        d = self.as_dict()
        lines = [
            "# Synthesis validation report",
            "",
            "Engineering facts about this run's generated environments (`awm env check_all`).",
            "",
            f"- environments: {d['environments_total']} total, {d['environments_started']} started, "
            f"{d['environments_failed']} failed",
            f"- tools (operationIds in generated code): {d['tools_total']}",
            "",
            "| failure category | count |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in sorted(self.failure_categories.items())],
        ]
        return "\n".join(lines) + "\n"


def parse_check_all(output: str, dataset_dir: Path) -> ValidationReport:
    catalog = {s.name: s.tools for s in build_catalog(dataset_dir)}
    started = PASSED.findall(output)
    failed = {m.group(1): m.group(2).strip() for m in FAILED.finditer(output)}
    cats = Counter(categorize(p) for p in failed.values())
    return ValidationReport(
        total=len(catalog),
        started=started,
        failed=failed,
        failure_categories=dict(cats),
        tools_per_env={name: catalog.get(name, 0) for name in started},
    )
