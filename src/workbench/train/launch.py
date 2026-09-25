"""`workbench train launch`: run the smoke profile in the separate train env (subprocess only).

Dry-run by default; `--execute` also requires every preflight check to pass. The run
directory always gets a NO_RESULTS marker (rule R2). The training process gets the allowlisted
environment of ``workbench.subprocess_env.train_env`` (ADR-026); the plan and ``env_names.json``
list the variable names it gets and the ones it does not, never their values.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from workbench.doctor import CheckResult
from workbench.subprocess_env import train_env
from workbench.train.profile import TrainProfile

NO_RESULTS_TEXT = (
    "NO_RESULTS\n\nThis directory comes from the SMOKE training profile (<=1.7B, LoRA, <=5 steps).\n"
    "It only demonstrates the rollout -> reward -> update loop. Nothing in here may be used for\n"
    "any effectiveness claim, and it is unrelated to Arctic-AWM or the AWM paper results.\n"
)


class LaunchError(RuntimeError):
    """Preflight failed or the profile is not launchable."""


def build_command(profile: TrainProfile, run_dir: Path, train_project: Path) -> list[str]:
    fixed = {
        "data.train_files": str(profile.train_files.resolve()),
        "data.val_files": str(profile.val_files.resolve()),
        "actor_rollout_ref.model.path": profile.model,
        "agent.init_config.model_name_or_path": profile.model,
        "trainer.experiment_name": run_dir.name,
        "trainer.default_local_dir": str((run_dir / "checkpoints").resolve()),
    }
    overrides = {**profile.overrides, **fixed}
    return [
        "uv",
        "run",
        "--project",
        str(train_project),
        "--no-sync",
        "python",
        "-m",
        "agentfly.cli",
        "train",
        *[f"{k}={_hydra(v)}" for k, v in overrides.items()],
    ]


def _hydra(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def prepare_run_dir(out_root: Path, profile: TrainProfile, command: list[str]) -> Path:
    run_dir = out_root / f"{time.strftime('%Y%m%d_%H%M%S')}_{profile.profile}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "NO_RESULTS").write_text(NO_RESULTS_TEXT, encoding="utf-8")
    (run_dir / "command.json").write_text(json.dumps(command, indent=1), encoding="utf-8")
    return run_dir


def env_names(env: Mapping[str, str], environ: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """The variable names the training process gets and the ones withheld from it (no values)."""
    source = os.environ if environ is None else environ
    return {"passed": sorted(env), "withheld": sorted(set(source) - set(env))}


def launch(
    profile: TrainProfile,
    out_root: Path,
    train_project: Path,
    preflight: list[CheckResult],
    execute: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan or run the smoke training in ``env`` (default ``train_env()``, ADR-026)."""
    env = dict(train_env() if env is None else env)
    placeholder = out_root / "<timestamp>_smoke"
    command = build_command(profile, placeholder, train_project)
    plan: dict[str, Any] = {
        "mode": "dry-run",
        "command": command,
        "env": env_names(env),
        "preflight": [c.__dict__ for c in preflight],
    }
    if not execute:
        return plan
    failed = [c.name for c in preflight if c.status == "fail"]
    if failed:
        raise LaunchError(f"preflight failed: {failed} (UNVERIFIED-LOCAL without a GPU)")
    run_dir = prepare_run_dir(out_root, profile, [])
    command = build_command(profile, run_dir, train_project)
    (run_dir / "command.json").write_text(json.dumps(command, indent=1), encoding="utf-8")
    (run_dir / "env_names.json").write_text(json.dumps(env_names(env), indent=1), encoding="utf-8")
    with (run_dir / "train.log").open("ab") as log:
        rc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=env, check=False).returncode
    return {"mode": "execute", "run_dir": str(run_dir), "returncode": rc}
