from pathlib import Path

import pytest

from workbench.config import Settings, get_settings


def test_yaml_values_loaded() -> None:
    s = get_settings()
    assert s.env.port_min == 18100
    assert s.llm.backend == "mock_replay"
    assert s.gateway.policy_file == Path("configs/tool_policy.yaml")


def test_env_var_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_LLM__BACKEND", "vllm")
    monkeypatch.setenv("WORKBENCH_ENV__MAX_ENVS", "7")
    s = Settings()
    assert s.llm.backend == "vllm"
    assert s.env.max_envs == 7


def test_custom_yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "app.yaml"
    cfg.write_text("api:\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setenv("WORKBENCH_CONFIG", str(cfg))
    s = Settings()
    assert s.api.port == 9999
    assert s.env.port_min == 18100  # untouched default


def test_init_kwargs_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_API__PORT", "1234")
    s = Settings(api={"port": 4321})  # type: ignore[arg-type]
    assert s.api.port == 4321
