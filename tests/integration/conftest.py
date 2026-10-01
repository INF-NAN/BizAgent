"""Integration fixtures: real AWM MCP server subprocesses on CPU using the hand-written mini
dataset (tests/fixtures/awm_mini — NOT official data). Tests against the official dataset
live in test_official_data.py and skip when data/awm1k is absent (CI)."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from workbench.config import EnvSettings
from workbench.envs.manager import EnvManager

MINI = Path("tests/fixtures/awm_mini")


def free_base(n: int = 30) -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1]) % 20000 + 30000


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[EnvManager]:
    base = free_base()
    m = EnvManager(
        EnvSettings(
            dataset_dir=MINI,
            runs_dir=tmp_path / "runs",
            port_min=base,
            port_max=base + 30,
            start_timeout_s=60,
        )
    )
    yield m
    await m.stop_all()
