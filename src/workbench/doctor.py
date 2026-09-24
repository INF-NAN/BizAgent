"""`workbench doctor`: environment self-check.

Each check returns a CheckResult. ``fail`` is blocking (non-zero exit); ``warn`` is
informational. A missing GPU or a not-yet-downloaded dataset is only a warning (rule R10:
everything must work on CPU with the mock backend).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from workbench.config import Settings

Status = Literal["ok", "warn", "fail"]

DATASET_FILES = ("gen_tasks.jsonl", "gen_db.jsonl", "gen_sample.jsonl", "gen_envs.jsonl")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def check_python(version: tuple[int, int] | None = None) -> CheckResult:
    major, minor = version or (sys.version_info.major, sys.version_info.minor)
    ok = (major, minor) == (3, 12)
    return CheckResult("python", "ok" if ok else "fail", f"{major}.{minor} (AWM and AgentFly require 3.12)")


def _git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return out.stdout.strip() or None


def check_submodule(name: str, path: Path, expected_sha: str) -> CheckResult:
    if not (path / ".git").exists():
        return CheckResult(
            f"submodule:{name}", "fail", f"{path} not initialized (git submodule update --init)"
        )
    head = _git_head(path)
    if head != expected_sha:
        return CheckResult(f"submodule:{name}", "fail", f"HEAD {head} != pinned {expected_sha[:12]}")
    return CheckResult(f"submodule:{name}", "ok", f"pinned at {expected_sha[:12]}")


def check_dataset(dataset_dir: Path) -> CheckResult:
    missing = [f for f in DATASET_FILES if not (dataset_dir / f).exists()]
    if missing:
        return CheckResult(
            "dataset", "warn", f"{dataset_dir}: missing {', '.join(missing)} (run `make data`)"
        )
    return CheckResult("dataset", "ok", f"{dataset_dir} present")


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def check_ports(settings: Settings) -> CheckResult:
    named = {"api": settings.api.port, "gateway": settings.gateway.port}
    busy = [f"{k}:{p}" for k, p in named.items() if port_in_use(settings.api.host, p)]
    pool = range(settings.env.port_min, settings.env.port_max + 1)
    busy_pool = sum(1 for p in pool if port_in_use(settings.env.host, p))
    detail = f"env pool {settings.env.port_min}-{settings.env.port_max}: {busy_pool} busy"
    if busy:
        return CheckResult("ports", "warn", f"in use: {', '.join(busy)}; {detail}")
    return CheckResult("ports", "ok", detail)


def check_gpu(which: Callable[[str], str | None] = shutil.which) -> CheckResult:
    if which("nvidia-smi") is None:
        return CheckResult(
            "gpu", "warn", "no nvidia-smi: GPU steps are UNVERIFIED-LOCAL (mock mode still works)"
        )
    return CheckResult("gpu", "ok", "nvidia-smi found")


def check_env_vars(settings: Settings, environ: dict[str, str] | None = None) -> CheckResult:
    env = environ if environ is not None else dict(os.environ)
    if settings.llm.backend == "openai_compat" and not env.get(settings.llm.api_key_env):
        return CheckResult(
            "env-vars", "fail", f"{settings.llm.api_key_env} is required by openai_compat backend"
        )
    return CheckResult("env-vars", "ok", f"backend={settings.llm.backend}")


def check_llm_backend(settings: Settings, client: httpx.Client | None = None) -> CheckResult:
    llm = settings.llm
    if llm.backend == "mock_replay":
        ok = llm.mock_fixture.exists()
        return CheckResult("llm", "ok" if ok else "fail", f"mock_replay fixture {llm.mock_fixture}")
    url = llm.base_url.rstrip("/") + "/models"
    headers = {}
    key = os.environ.get(llm.api_key_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(llm.connect_timeout_s))
    try:
        resp = http.get(url, headers=headers)
        if resp.status_code >= 400:
            return CheckResult("llm", "fail", f"{url} -> HTTP {resp.status_code}")
        return CheckResult("llm", "ok", f"{url} reachable")
    except httpx.HTTPError as exc:
        return CheckResult("llm", "fail", f"{url} unreachable: {type(exc).__name__}")
    finally:
        if own:
            http.close()


def run_checks(settings: Settings) -> list[CheckResult]:
    up = settings.upstream
    return [
        check_python(),
        check_submodule("agent-world-model", up.awm_dir, up.awm_sha),
        check_submodule("AgentFly", up.agentfly_dir, up.agentfly_sha),
        check_dataset(settings.env.dataset_dir),
        check_ports(settings),
        check_gpu(),
        check_env_vars(settings),
        check_llm_backend(settings),
    ]


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0
