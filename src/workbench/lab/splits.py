"""Deterministic scenario-level split of the official dataset.

Scenarios, not tasks, are split: test and validation tasks come from scenarios that no training
trajectory ever touched, so a gain on the test split is a gain on unseen environments.
Only tasks that have a pure-code verifier are eligible, and the matching verifier entry (the
first one, as `awm verify` picks it) is copied next to the split so each episode can be verified
without loading the whole verifier file.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SPLITS = ("train", "val", "test")
CODE_VERIFIERS = "gen_verifier.pure_code.jsonl"


@dataclass(frozen=True)
class LabTask:
    key: str
    scenario: str
    task_id: int
    task: str
    split: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_tasks(dataset_dir: Path) -> dict[str, list[str]]:
    return {row["scenario"]: list(row["tasks"]) for row in _jsonl(dataset_dir / "gen_tasks.jsonl")}


def load_verifier_index(dataset_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """First code-verifier entry per (scenario, task_idx), like awm's find_scenario_entry."""
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for row in _jsonl(dataset_dir / CODE_VERIFIERS):
        key = (row["scenario"], int(row["task_idx"]))
        index.setdefault(key, row)
    return index


def make_split(
    tasks: dict[str, list[str]],
    verifiers: dict[tuple[str, int], dict[str, Any]],
    *,
    seed: int,
    scenario_counts: dict[str, int],
    task_counts: dict[str, int],
) -> dict[str, list[LabTask]]:
    scenarios = sorted(s for s, ts in tasks.items() if any((s, i) in verifiers for i in range(len(ts))))
    rng = random.Random(seed)
    rng.shuffle(scenarios)
    groups: dict[str, list[str]] = {}
    start = 0
    for split in ("test", "val", "train"):
        n = scenario_counts.get(split, 0) if split != "train" else len(scenarios) - start
        groups[split] = sorted(scenarios[start : start + n])
        start += n
    out: dict[str, list[LabTask]] = {}
    for split in SPLITS:
        pool = [
            LabTask(f"{s}#{i}", s, i, text, split)
            for s in groups[split]
            for i, text in enumerate(tasks[s])
            if (s, i) in verifiers
        ]
        rng_split = random.Random(f"{seed}:{split}")
        rng_split.shuffle(pool)
        out[split] = pool[: task_counts.get(split, len(pool))]
    return out


def write_split(
    out_dir: Path,
    split: dict[str, list[LabTask]],
    verifiers: dict[tuple[str, int], dict[str, Any]],
    seed: int,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "seed": seed,
        "scenarios": {k: sorted({t.scenario for t in v}) for k, v in split.items()},
        "tasks": {k: [t.as_dict() for t in v] for k, v in split.items()},
    }
    path = out_dir / "splits.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out_dir / "verifiers.code.jsonl").open("w", encoding="utf-8") as f:
        for tasks_ in split.values():
            for t in tasks_:
                f.write(json.dumps(verifiers[(t.scenario, t.task_id)], ensure_ascii=False) + "\n")
    return path


def read_split(out_dir: Path, name: str) -> list[LabTask]:
    doc = json.loads((out_dir / "splits.json").read_text(encoding="utf-8"))
    return [LabTask(**t) for t in doc["tasks"][name]]


def read_verifiers(out_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    return {(r["scenario"], int(r["task_idx"])): r for r in _jsonl(out_dir / "verifiers.code.jsonl")}
