"""Post-synthesis validation against real `awm env reset_db` + `awm env check_all` (no LLM)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.integration.conftest import MINI
from workbench.synth.runner import default_command_runner, validate_run

pytestmark = pytest.mark.integration


def test_validate_run_on_generated_like_dir(tmp_path: Path) -> None:
    run = tmp_path / "synth" / "fake_run"
    run.mkdir(parents=True)
    for name in ("gen_db.jsonl", "gen_sample.jsonl"):
        shutil.copy(MINI / name, run / name)
    env_row = json.loads((MINI / "gen_envs.jsonl").read_text().splitlines()[0])
    env_row["db_path"] = str(
        run / "databases" / "mini_e_commerce.db"
    )  # as `awm gen env --database_dir` writes it
    (run / "gen_envs.jsonl").write_text(json.dumps(env_row) + "\n")
    env = dict(os.environ)
    env.setdefault("PYTHONPYCACHEPREFIX", str(Path(".cache/pycache").resolve()))
    report = validate_run(run, default_command_runner, env).as_dict()
    assert report["environments_total"] == 1
    assert report["environments_started"] == 1 and report["tools_total"] == 7
    assert (run / "validation.md").exists() and (run / "databases" / "mini_e_commerce.db").exists()
