"""The train env's processes get an allowlisted environment (ADR-026, owner decision D13b).

Real subprocesses: `launch` runs a probe instead of the training command (there is no GPU here)
and the probe reports the variable names it sees; the values planted as secrets must not reach it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from workbench.doctor import CheckResult
from workbench.subprocess_env import BASE_VARS, train_env
from workbench.train import launch as launch_module
from workbench.train.launch import launch
from workbench.train.preflight import env_runner
from workbench.train.profile import TrainProfile

MARK = "planted-not-a-real-secret"  # pragma: allowlist secret
SECRETS = {
    name: MARK
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "WANDB_API_KEY",
        "SWANLAB_API_KEY",
        "VOLC_SECRET_ACCESS_KEY",
        "VLLM_API_KEY",
        "RAY_AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GH_TOKEN",
        "WORKBENCH_APPROVAL_SECRET",
    )
}
NEEDED = {  # what a GPU machine may rely on (values are placeholders)
    "PATH": os.environ["PATH"],
    "HOME": "/root",
    "LD_LIBRARY_PATH": "/usr/local/nvidia/lib64",
    "CUDA_VISIBLE_DEVICES": "0",
    "CUDA_HOME": "/usr/local/cuda",
    "NCCL_P2P_DISABLE": "1",
    "HF_ENDPOINT": "https://hf-mirror.com",
    "HF_HOME": "/root/autodl-tmp/hf",
    "HF_HUB_OFFLINE": "1",
    "VLLM_USE_V1": "1",
    "RAY_TMPDIR": "/tmp/ray",
    "VERL_LOGGING_LEVEL": "INFO",
    "TORCH_CUDA_ARCH_LIST": "8.0",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "UV_CACHE_DIR": "/root/autodl-tmp/uv",
    "http_proxy": "http://127.0.0.1:3128",
    "TOKENIZERS_PARALLELISM": "false",
}
UNRELATED = {"GIT_CONFIG_VALUE_0": "Authorization: Basic xyz", "SOME_TOOL_SETTING": "1"}
ENVIRON = {**SECRETS, **NEEDED, **UNRELATED}

PROBE = (
    "import json, os; print(json.dumps({'names': sorted(os.environ), "
    f"'planted': sorted(k for k, v in os.environ.items() if v == {MARK!r})}}))"
)


def test_train_env_keeps_what_the_stack_needs_and_drops_the_rest() -> None:
    env = train_env(ENVIRON)
    assert set(NEEDED) <= set(env)
    assert not set(SECRETS) & set(env)  # credential-like names, even under HF_, VLLM_, RAY_
    assert not set(UNRELATED) & set(env)  # deny-first: not listed, not passed
    assert env["PYTHONPYCACHEPREFIX"].endswith(".cache/pycache")  # no __pycache__ in submodules
    assert set(env) <= {*BASE_VARS, *NEEDED}


def test_passthrough_adds_names_on_purpose() -> None:
    env = train_env(ENVIRON, passthrough=["HF_TOKEN", "SOME_TOOL_SETTING", "NOT_SET"])
    assert env["HF_TOKEN"] == MARK and env["SOME_TOOL_SETTING"] == "1" and "NOT_SET" not in env
    assert "DEEPSEEK_API_KEY" not in env


def launch_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> dict[str, list[str]]:
    """`launch --execute` with the training command replaced by PROBE; what the probe saw."""
    monkeypatch.setattr(launch_module, "build_command", lambda *a: [sys.executable, "-c", PROBE])
    ok = [CheckResult(name, "ok", "") for name in ("gpu", "train-env", "verl")]
    result = launch(
        TrainProfile.load(Path("configs/train/smoke.yaml")), tmp_path, Path("train"), ok, True, env
    )
    run_dir = Path(result["run_dir"])
    assert result["returncode"] == 0 and (run_dir / "NO_RESULTS").exists()
    seen: dict[str, list[str]] = json.loads((run_dir / "train.log").read_text().splitlines()[-1])
    names = json.loads((run_dir / "env_names.json").read_text())
    assert MARK not in (run_dir / "env_names.json").read_text()  # names only, never values
    # the probe got exactly the passed names; its interpreter may add LC_CTYPE (PEP 538 coercion)
    assert set(names["passed"]) <= set(seen["names"])
    assert set(seen["names"]) - set(names["passed"]) <= {"LC_CTYPE"}
    return seen


def test_control_the_full_environment_reaches_the_training_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # what `launch` did before ADR-026: the training process inherited everything
    seen = launch_probe(tmp_path, monkeypatch, {**os.environ, **ENVIRON})
    assert seen["planted"] == sorted(SECRETS)


def test_launch_runs_training_in_the_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = launch_probe(tmp_path, monkeypatch, train_env(ENVIRON))
    assert seen["planted"] == [] and set(NEEDED) <= set(seen["names"])


def test_launch_defaults_to_the_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    plan = launch(TrainProfile.load(Path("configs/train/smoke.yaml")), tmp_path, Path("train"), [])
    assert not set(SECRETS) & set(plan["env"]["passed"]) and set(SECRETS) <= set(plan["env"]["withheld"])


def test_preflight_probes_see_what_training_will_see() -> None:
    rc, out = env_runner(train_env(ENVIRON))([sys.executable, "-c", PROBE])
    seen = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and seen["planted"] == [] and set(NEEDED) <= set(seen["names"])
