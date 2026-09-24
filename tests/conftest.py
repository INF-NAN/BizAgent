"""Shared pytest setup. All tests run on CPU with the mock LLM backend (rule R10)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
# Keep bytecode out of the upstream submodules so their worktrees stay clean (rule R5).
os.environ.setdefault("PYTHONPYCACHEPREFIX", str(ROOT / ".cache" / "pycache"))

MINI_DATASET = ROOT / "tests" / "fixtures" / "awm_mini"
FIXTURES = ROOT / "tests" / "fixtures"
OFFICIAL_DATASET = ROOT / "data" / "awm1k"
OFFICIAL_FILES = (
    "gen_scenario.jsonl",
    "gen_tasks.jsonl",
    "gen_db.jsonl",
    "gen_sample.jsonl",
    "gen_spec.jsonl",
    "gen_envs.jsonl",
    "gen_verifier.jsonl",
    "gen_verifier.pure_code.jsonl",
)


def official_data_present() -> bool:
    return all((OFFICIAL_DATASET / f).is_file() for f in OFFICIAL_FILES)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # CI never downloads the official dataset: official_data tests skip there (TASK_v2 Phase 11).
    if official_data_present():
        return
    skip = pytest.mark.skip(reason="official dataset not found in data/awm1k (run `make data`)")
    for item in items:
        if "official_data" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from workbench.config import reset_settings_cache

    for key in list(os.environ):
        if key.startswith("WORKBENCH_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()
