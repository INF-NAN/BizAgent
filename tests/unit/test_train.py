from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from workbench.train.launch import NO_RESULTS_TEXT, LaunchError, build_command, launch, prepare_run_dir
from workbench.train.preflight import (
    check_data,
    check_gpu,
    check_override_keys,
    check_train_env,
    check_verl,
    hydra_compose_check,
    run_preflight,
)
from workbench.train.profile import ProfileError, TrainProfile

SMOKE = Path("configs/train/smoke.yaml")


def test_smoke_profile_respects_r2() -> None:
    p = TrainProfile.load(SMOKE)
    assert p.profile == "smoke" and p.model_size_b <= 1.7 and p.no_results
    assert int(p.overrides["trainer.total_training_steps"]) <= 5
    assert int(p.overrides["actor_rollout_ref.model.lora_rank"]) > 0


@pytest.mark.parametrize(
    ("patch", "msg"),
    [
        ({"profile": "full"}, "only the 'smoke'"),
        ({"model_size_b": 7.0}, "model_size_b"),
        ({"no_results": False}, "no_results"),
        (
            {"overrides": {"trainer.total_training_steps": 100, "actor_rollout_ref.model.lora_rank": 8}},
            "total_training_steps",
        ),
        ({"overrides": {"trainer.total_training_steps": 3}}, "LoRA required"),
    ],
)
def test_profile_rejects_violations(tmp_path: Path, patch: dict, msg: str) -> None:  # type: ignore[type-arg]
    import yaml

    raw = yaml.safe_load(SMOKE.read_text())
    raw.update(patch)
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match=msg):
        TrainProfile.load(f)


def fake_runner(outputs: dict[str, tuple[int, str]]):  # type: ignore[no-untyped-def]
    def run(cmd: list[str]) -> tuple[int, str]:
        for key, value in outputs.items():
            if key in " ".join(cmd):
                return value
        return 127, "not found"

    return run


def test_gpu_check_mocked() -> None:
    ok = fake_runner({"nvidia-smi": (0, "NVIDIA A100, 81920, 80000\n")})
    assert check_gpu(ok).status == "ok"
    small = fake_runner({"nvidia-smi": (0, "T4, 15360, 4000\n")})
    assert check_gpu(small).status == "fail"
    assert "UNVERIFIED-LOCAL" in check_gpu(fake_runner({})).detail


def test_train_env_probe_mocked() -> None:
    probe = json.dumps({"torch": "2.10.0", "cuda": "12.8", "cuda_available": True, "vllm": "0.19.0"})
    r = check_train_env(fake_runner({"import json, torch": (0, probe + "\n")}), Path("train"))
    assert r.status == "ok" and "vllm 0.19.0" in r.detail
    assert check_train_env(fake_runner({}), Path("train")).status == "fail"


def test_hydra_compose_check_mocked() -> None:
    assert (
        hydra_compose_check(
            fake_runner({"compose": (0, "composed\n")}), Path("train"), Path("cfg"), []
        ).status
        == "ok"
    )
    assert (
        hydra_compose_check(
            fake_runner({"compose": (1, "ConfigKeyError")}), Path("train"), Path("cfg"), []
        ).status
        == "fail"
    )


def test_override_keys_tolerate_conflict_markers(tmp_path: Path) -> None:
    f = tmp_path / "_generated_ppo_trainer.yaml"
    f.write_text(
        "trainer:\n  total_training_steps: null\n"
        "<<<<<<< HEAD\n  logger: a\n=======\n  logger: b\n>>>>>>> main\n"
    )
    assert check_override_keys(["trainer.total_training_steps", "trainer.logger"], [f]).status == "ok"
    bad = check_override_keys(["agent.max_turns"], [f])
    assert bad.status == "fail" and "agent.max_turns" in bad.detail
    assert check_override_keys(["x"], [tmp_path / "missing.yaml"]).status == "fail"


def test_verl_and_data_checks(tmp_path: Path) -> None:
    assert check_verl(tmp_path).status == "fail"
    (tmp_path / "verl/verl/version").mkdir(parents=True)
    (tmp_path / "verl/verl/version/version").write_text("0.8.0.dev")
    assert check_verl(tmp_path).status == "ok"
    assert check_data(TrainProfile.load(SMOKE)).status == "ok"


def test_launch_dry_run_and_refusal(tmp_path: Path) -> None:
    p = TrainProfile.load(SMOKE)
    checks = run_preflight(p, Path("third_party/AgentFly"), Path("train"), run=fake_runner({}))
    assert {c.name for c in checks if c.status == "fail"} >= {"gpu", "train-env"}  # no GPU here
    plan = launch(p, tmp_path, Path("train"), checks)
    cmd = plan["command"]
    assert cmd[:4] == ["uv", "run", "--project", "train"] and cmd[5:9] == [
        "python",
        "-m",
        "agentfly.cli",
        "train",
    ]
    assert "actor_rollout_ref.model.lora_rank=8" in cmd and "trainer.total_training_steps=3" in cmd
    assert "trainer.val_before_train=false" in cmd
    with pytest.raises(LaunchError, match="preflight failed"):
        launch(p, tmp_path, Path("train"), checks, execute=True)
    assert not any(tmp_path.iterdir())  # nothing was started


def test_run_dir_marked_no_results(tmp_path: Path) -> None:
    p = TrainProfile.load(SMOKE)
    run_dir = prepare_run_dir(tmp_path, p, build_command(p, tmp_path / "x", Path("train")))
    assert (run_dir / "NO_RESULTS").read_text() == NO_RESULTS_TEXT
    assert run_dir.name.endswith("_smoke")


FORBIDDEN = ("torch", "verl", "agentfly", "vllm", "ray", "transformers", "deepspeed")


def test_app_code_never_imports_training_deps() -> None:
    """Rule R11: the app environment must not import training dependencies."""
    offenders = []
    for py in Path("src/workbench").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{py}:{n}" for n in names if n.split(".")[0] in FORBIDDEN]
    assert offenders == []
