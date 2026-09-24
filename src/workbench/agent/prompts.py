"""Versioned prompt files (src/workbench/agent/prompts/*.md). Changes go to docs/CHANGELOG.md."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADER = re.compile(r"<!--\s*prompt:\s*(?P<name>\w+)\s*\|\s*version:\s*(?P<version>\d+)\s*-->")
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str


def load_prompt(name: str, directory: Path = PROMPTS_DIR) -> Prompt:
    raw = (directory / f"{name}.md").read_text(encoding="utf-8")
    m = HEADER.search(raw.splitlines()[0] if raw else "")
    if not m or m.group("name") != name:
        raise ValueError(f"{name}.md must start with '<!-- prompt: {name} | version: N -->'")
    body = raw.split("\n", 1)[1].strip() if "\n" in raw else ""
    return Prompt(name, int(m.group("version")), body)
