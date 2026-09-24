"""Children that run generated code see no secrets (ADR-019): real child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.unit.secrets_harness import PROBE, leaked, plant, unexpected
from workbench.cli import app
from workbench.subprocess_env import BASE_VARS, generated_code_env


def child_view(env: dict[str, str]) -> dict[str, Any]:
    out = subprocess.run(
        [sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, check=True, timeout=60
    )
    view: dict[str, Any] = json.loads(out.stdout)
    return view


def test_allowlist_keeps_only_base_vars() -> None:
    environ = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/root",
        "HOST": "0.0.0.0",
        "HTTPS_PROXY": "http://proxy:3128",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic x",
        "DEEPSEEK_API_KEY": "x",  # pragma: allowlist secret
    }
    env = generated_code_env(environ)
    assert set(env) == {"PATH", "LANG", "PYTHONPYCACHEPREFIX"}
    assert env["PYTHONPYCACHEPREFIX"] == str(Path(".cache/pycache").resolve())
    assert generated_code_env({"PYTHONPYCACHEPREFIX": "/p"})["PYTHONPYCACHEPREFIX"] == "/p"
    assert "PATH" in BASE_VARS and "HOME" not in BASE_VARS


def test_child_process_sees_no_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    plant(monkeypatch)
    before = child_view(dict(os.environ))  # the old `dict(os.environ)` hand-over, as a control
    assert "DEEPSEEK_API_KEY" in before["names"] and before["planted"]
    after = child_view(generated_code_env())
    assert leaked(after["names"]) == [] and not after["planted"]
    assert unexpected(after["names"]) == [] and "PATH" in after["names"]


class RecordingAwm:
    """Fake `python -m awm.cli ...`: records each step's environment, writes its outputs."""

    def __init__(self) -> None:
        self.envs: dict[str, dict[str, str]] = {}

    def __call__(self, argv: list[str], env: dict[str, str], log: Path) -> int:
        group = "gen" if "gen" in argv else "env"
        self.envs[argv[argv.index(group) + 1]] = dict(env)
        for flag in ("--output", "--output_path"):
            if flag in argv:
                Path(argv[argv.index(flag) + 1]).write_text('{"scenario": "s"}\n', encoding="utf-8")
        return 0


def synth_environ() -> dict[str, str]:
    return {
        **os.environ,
        "AWM_SYN_OVERRIDE_MODEL": "m",
        "EMBEDDING_OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],  # pragma: allowlist secret
        "HTTPS_PROXY": "http://proxy:3128",
    }


def test_gen_steps_get_a_placeholder_key_and_validation_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.config import Settings
    from workbench.synth.runner import STEPS, SynthRunner

    plant(monkeypatch)
    fake = RecordingAwm()
    runner = SynthRunner(
        Settings(synth={"out_dir": tmp_path / "synth"}),  # type: ignore[arg-type]
        tmp_path / "synth" / "r",
        scenarios=1,
        command_runner=fake,
        environ=synth_environ(),
    )
    runner.execute(proxy_base="http://127.0.0.1:9")
    llm_extra = (
        "AWM_SYN_LLM_PROVIDER",
        "AWM_SYN_OVERRIDE_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "EMBEDDING_OPENAI_API_KEY",
        "EMBEDDING_OPENAI_BASE_URL",
    )
    for step in STEPS:  # all call the LLM; `gen env` and `gen verifier` also run generated code
        env = fake.envs[step]
        assert leaked(env) == ["EMBEDDING_OPENAI_API_KEY", "OPENAI_API_KEY"]
        assert env["OPENAI_API_KEY"] == env["EMBEDDING_OPENAI_API_KEY"] == "workbench-proxy"
        assert unexpected(env, llm_extra) == []  # no HTTPS_PROXY: the proxy is local
    for step in ("reset_db", "check_all"):  # generated SQL / server code, no LLM
        assert leaked(fake.envs[step]) == [] and unexpected(fake.envs[step]) == []


def test_direct_gen_step_gets_llm_and_network_settings_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.config import Settings
    from workbench.synth.runner import SynthRunner

    plant(monkeypatch)
    runner = SynthRunner(
        Settings(synth={"out_dir": tmp_path / "synth"}),  # type: ignore[arg-type]
        tmp_path / "synth" / "r",
        scenarios=1,
        environ=synth_environ(),
    )
    env = runner.step_env("task", None)
    assert leaked(env) == ["EMBEDDING_OPENAI_API_KEY", "OPENAI_API_KEY"]
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["AWM_SYN_OVERRIDE_MODEL"] == "m"


def test_synth_validate_command_passes_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(monkeypatch)
    seen: dict[str, str] = {}

    def fake_validate(run_dir: Path, run: Any, env: dict[str, str]) -> Any:
        seen.update(env)
        return SimpleNamespace(as_dict=lambda: {"environments_total": 0})

    monkeypatch.setattr("workbench.synth.runner.validate_run", fake_validate)
    result = CliRunner().invoke(app, ["synth", "validate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert leaked(seen) == [] and unexpected(seen) == [] and "PATH" in seen
