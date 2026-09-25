"""`workbench train preflight`: GPU, CUDA/torch/vLLM/veRL versions, data paths, config keys.

All probes of the train env run as subprocesses (`uv run --project train --no-sync ...`) and
are injectable for tests (GPU info is mocked in CI: there is no GPU, rule R10). The CLI runs them
with the environment training will get (``env_runner(train_env())``, ADR-026), so preflight
checks what `launch` will see.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from workbench.doctor import CheckResult, Status
from workbench.subprocess_env import train_env
from workbench.train.profile import TrainProfile

Runner = Callable[[list[str]], tuple[int, str]]

PROBE = (
    "import json, torch, vllm; "
    "print(json.dumps({'torch': torch.__version__, 'cuda': torch.version.cuda, "
    "'cuda_available': torch.cuda.is_available(), 'vllm': vllm.__version__}))"
)


def env_runner(env: Mapping[str, str] | None = None) -> Runner:
    """A runner whose probes get ``env`` (None: this process's environment)."""

    def run(cmd: list[str]) -> tuple[int, str]:
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=None if env is None else dict(env),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return out.returncode, out.stdout + out.stderr

    return run


def check_gpu(run: Runner, min_free_mb: int = 12_000) -> CheckResult:
    rc, out = run(
        ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"]
    )
    if rc != 0:
        return CheckResult("gpu", "fail", "nvidia-smi unavailable: UNVERIFIED-LOCAL (needs a CUDA GPU)")
    gpus = [line.split(",") for line in out.strip().splitlines() if line.strip()]
    if not gpus:
        return CheckResult("gpu", "fail", "no GPU listed by nvidia-smi")
    best = max(int(g[2]) for g in gpus)
    status: Status = "ok" if best >= min_free_mb else "fail"
    names = ", ".join(g[0].strip() for g in gpus)
    return CheckResult(
        "gpu", status, f"{len(gpus)} GPU(s) [{names}], max free {best} MiB (need {min_free_mb})"
    )


def check_train_env(run: Runner, project: Path) -> CheckResult:
    rc, out = run(["uv", "run", "--project", str(project), "--no-sync", "python", "-c", PROBE])
    if rc != 0:
        return CheckResult(
            "train-env", "fail", f"train env not installed/importable (cd {project} && uv sync): {out[-200:]}"
        )
    info: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
    status: Status = "ok" if info.get("cuda_available") else "fail"
    return CheckResult(
        "train-env",
        status,
        f"torch {info['torch']} (cuda {info['cuda']}), vllm {info['vllm']}, "
        f"cuda_available={info['cuda_available']}",
    )


def check_verl(agentfly_dir: Path) -> CheckResult:
    version = agentfly_dir / "verl" / "verl" / "version" / "version"
    if not version.exists():
        return CheckResult(
            "verl",
            "fail",
            "AgentFly's nested verl submodule is not initialized "
            "(git -C third_party/AgentFly submodule update --init verl; its URL is SSH-form)",
        )
    return CheckResult("verl", "ok", f"verl {version.read_text().strip()}")


def check_data(profile: TrainProfile) -> CheckResult:
    missing = [str(p) for p in (profile.train_files, profile.val_files) if not p.exists()]
    if missing:
        return CheckResult("data", "fail", f"missing {missing}")
    rows = json.loads(profile.train_files.read_text(encoding="utf-8"))
    bad = [i for i, r in enumerate(rows) if "question" not in r or "answer" not in r]
    if bad:
        return CheckResult("data", "fail", f"rows {bad[:5]} lack question/answer")
    return CheckResult("data", "ok", f"{len(rows)} rows in {profile.train_files}")


def _flatten(tree: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(tree, dict):
        for k, v in tree.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys.add(path)
            keys |= _flatten(v, path)
    return keys


CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")


def load_yaml_tolerant(path: Path) -> Any:
    """Parse YAML, dropping git conflict-marker lines (keeps both sides).

    The pinned veRL fork (001f000) ships `_generated_ppo_trainer.yaml` with unresolved merge
    markers (lines 177-185, docs/RECON.md §10 Phase 7). That file is a flattened reference,
    not what Hydra loads, so for a static key check the union of both sides is acceptable;
    the authoritative check is `hydra_compose_check` inside the train env.
    """
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith(CONFLICT_MARKERS)]
    return yaml.safe_load("\n".join(lines))


def check_override_keys(overrides: Iterable[str], config_files: list[Path]) -> CheckResult:
    """Every Hydra override must name a key that exists in the pinned veRL config (static)."""
    present = [f for f in config_files if f.exists()]
    if not present:
        return CheckResult(
            "config-keys", "fail", "veRL config files not found (verl submodule not initialized)"
        )
    known: set[str] = set()
    for f in present:
        known |= _flatten(load_yaml_tolerant(f))
    unknown = sorted(k for k in overrides if k not in known)
    if unknown:
        return CheckResult("config-keys", "fail", f"unknown Hydra keys: {unknown}")
    return CheckResult(
        "config-keys", "ok", f"{len(list(overrides))} override keys exist in the pinned config"
    )


HYDRA_COMPOSE = (
    "import json, sys; from hydra import compose, initialize_config_dir; "
    "d, o = sys.argv[1], json.loads(sys.argv[2]); "
    "initialize_config_dir(config_dir=d, version_base=None); "
    "compose(config_name='ppo_trainer', overrides=o); print('composed')"
)


def hydra_compose_check(
    run: Runner, train_project: Path, config_dir: Path, overrides: list[str]
) -> CheckResult:
    """Authoritative: let Hydra itself compose the config with our overrides (strict keys)."""
    cmd = [
        "uv",
        "run",
        "--project",
        str(train_project),
        "--no-sync",
        "python",
        "-c",
        HYDRA_COMPOSE,
        str(config_dir.resolve()),
        json.dumps(overrides),
    ]
    rc, out = run(cmd)
    if rc != 0 or "composed" not in out:
        return CheckResult(
            "hydra-compose", "fail", f"Hydra compose failed or train env missing: {out[-300:]}"
        )
    return CheckResult("hydra-compose", "ok", "Hydra composed ppo_trainer with all overrides")


def verl_config_files(agentfly_dir: Path) -> list[Path]:
    base = agentfly_dir / "verl" / "verl" / "trainer" / "config"
    return [base / "_generated_ppo_trainer.yaml", base / "ppo_trainer.yaml"]


def run_preflight(
    profile: TrainProfile, agentfly_dir: Path, train_project: Path, run: Runner | None = None
) -> list[CheckResult]:
    """All checks; the probes run in ``train_env()`` unless ``run`` says otherwise (ADR-026)."""
    run = run or env_runner(train_env())
    keys = [
        *profile.overrides,
        "data.train_files",
        "data.val_files",
        "actor_rollout_ref.model.path",
        "agent.init_config.model_name_or_path",
        "trainer.experiment_name",
        "trainer.default_local_dir",
    ]
    return [
        CheckResult(
            "profile",
            "ok",
            f"{profile.profile}: {profile.model} ({profile.model_size_b}B), LoRA, "
            f"{profile.overrides.get('trainer.total_training_steps')} steps, NO_RESULTS",
        ),
        check_gpu(run),
        check_train_env(run, train_project),
        check_verl(agentfly_dir),
        check_override_keys(keys, verl_config_files(agentfly_dir)),
        hydra_compose_check(
            run,
            train_project,
            agentfly_dir / "verl" / "verl" / "trainer" / "config",
            [f"{k}={v}" for k, v in profile.overrides.items()],
        ),
        check_data(profile),
    ]
