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


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from workbench.config import reset_settings_cache

    for key in list(os.environ):
        if key.startswith("WORKBENCH_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()
