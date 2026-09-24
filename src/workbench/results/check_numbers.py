"""Number guard (`make check-numbers`): no unregistered performance-like numbers in README/docs.

A token counts as "performance-like" if it is a percentage, a two-decimal score (x.xx) or a
Pass@k value. Each such token must be a registry value (with the paper disclaimer on the
same page) or be whitelisted in configs/number_whitelist.yaml with a reason. Application-
layer effectiveness wording (R3) is rejected outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from workbench.results.registry import DISCLAIMER, Registry

PERCENT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?%")
SCORE = re.compile(r"(?<![\w.])\d{1,3}\.\d{2}(?![\d.])")
VERSION_SPEC = re.compile(r"(?:[<>=~^]=?|!=)\s*$")  # `>=2.11`, `<0.47`: dependency specifiers
PASS_AT_K = re.compile(r"pass@\d+\s*[:=]?\s*\d+(?:\.\d+)?", re.I)
FORBIDDEN = [
    "准确率提升",
    "成功率提升",
    "可靠性提升",
    "success rate improvement",
    "accuracy improvement",
]


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    token: str
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.token!r} — {self.reason}"


def load_whitelist(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tokens": {}, "files": {}}
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"tokens": raw.get("tokens") or {}, "files": raw.get("files") or {}}


def default_targets(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def scan(files: list[Path], registry: Registry, whitelist: dict[str, Any], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    allowed_tokens = {str(k) for k in whitelist["tokens"]}
    registry_values = registry.printed_values
    for f in files:
        if not f.exists():
            continue
        rel = str(f.relative_to(root))
        file_allowed = {str(t) for t in (whitelist["files"].get(rel) or {})}
        text = f.read_text(encoding="utf-8")
        cites_registry = False
        for n, line in enumerate(text.splitlines(), start=1):
            for phrase in FORBIDDEN:
                if phrase in line:
                    findings.append(
                        Finding(rel, n, phrase, "application-layer effectiveness claim (rule R3)")
                    )
            for rx in (PERCENT, SCORE, PASS_AT_K):
                for m in rx.finditer(line):
                    token = m.group(0).strip()
                    if VERSION_SPEC.search(line[: m.start()]):
                        continue
                    if token in registry_values:
                        cites_registry = True
                        continue
                    if token in allowed_tokens or token in file_allowed:
                        continue
                    findings.append(
                        Finding(rel, n, token, "performance-like number not in registry or whitelist")
                    )
        if cites_registry and DISCLAIMER not in text:
            findings.append(
                Finding(rel, 0, "(registry value)", "paper numbers cited without the R3 disclaimer")
            )
    return findings
