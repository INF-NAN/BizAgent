"""Central configuration.

Every port, path, timeout, concurrency limit and backend choice lives here (Phase 1 rule:
no other module hard-codes them). Precedence, highest first:
init kwargs > environment variables (``WORKBENCH_`` prefix, ``__`` nesting) > ``.env`` >
``configs/app.yaml`` (path overridable with ``WORKBENCH_CONFIG``) > defaults below.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = Path("configs/app.yaml")


class UpstreamSettings(BaseModel):
    awm_dir: Path = Path("third_party/agent-world-model")
    awm_sha: str = "85e322f69279e3b3325b7377ec3bab788514e9cb"
    agentfly_dir: Path = Path("third_party/AgentFly")
    agentfly_sha: str = "1256586b1109ba8e0dc0f179f8515b4567d09df4"


class EnvSettings(BaseModel):
    dataset_dir: Path = Path("data/awm1k")
    runs_dir: Path = Path("data/runs")
    host: str = "127.0.0.1"
    port_min: int = 18100
    port_max: int = 18199
    max_envs: int = 4
    queue_timeout_s: float = 60.0
    start_timeout_s: float = 60.0
    health_timeout_s: float = 10.0
    health_poll_s: float = 0.5
    idle_timeout_s: float = 600.0
    reap_interval_s: float = 5.0
    stop_grace_s: float = 5.0
    # When set, the app talks to a separate env-manager service (docker compose) instead of
    # managing subprocesses in-process.
    manager_url: str | None = None
    manager_host: str = "127.0.0.1"
    manager_port: int = 8090


class GatewaySettings(BaseModel):
    policy_file: Path = Path("configs/tool_policy.yaml")
    audit_path: Path = Path("data/audit/gateway.jsonl")
    upstream_timeout_s: float = 30.0
    approval_ttl_s: float = 900.0
    approval_secret_env: str = "WORKBENCH_APPROVAL_SECRET"
    host: str = "127.0.0.1"
    port: int = 8081
    rate_capacity: float = 10.0
    rate_refill_per_s: float = 2.0
    summary_max_chars: int = 300


class LLMSettings(BaseModel):
    backend: Literal["mock_replay", "vllm", "openai_compat"] = "mock_replay"
    model: str = "Snowflake/Arctic-AWM-4B"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key_env: str = "OPENAI_API_KEY"
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 60.0
    total_timeout_s: float = 180.0
    max_retries: int = 3
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    temperature: float = 0.6
    max_tokens: int = 2048
    mock_fixture: Path = Path("tests/fixtures/trajectories/e_commerce_33_basic.jsonl")


class AgentSettings(BaseModel):
    max_steps: int = 12
    repeat_call_threshold: int = 3
    no_change_threshold: int = 4
    token_budget: int = 60_000
    wall_clock_s: float = 300.0
    plan_retries: int = 2
    checkpoint_db: Path = Path("data/agent/checkpoints.sqlite")
    memory_db: Path = Path("data/agent/memory.sqlite")
    memory_ttl_s: float = 30 * 24 * 3600.0
    prompts_dir: Path = Path("src/workbench/agent/prompts")


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    max_sessions: int = 4
    ui_dir: Path = Path("ui")


class SynthSettings(BaseModel):
    out_dir: Path = Path("data/synth")
    pricing_file: Path = Path("configs/pricing.yaml")
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8095
    upstream_base_url_env: str = "OPENAI_BASE_URL"
    upstream_api_key_env: str = "OPENAI_API_KEY"


class TrainSettings(BaseModel):
    project_dir: Path = Path("train")
    smoke_config: Path = Path("configs/train/smoke.yaml")
    out_dir: Path = Path("data/train_runs")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WORKBENCH_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    env: EnvSettings = Field(default_factory=EnvSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    synth: SynthSettings = Field(default_factory=SynthSettings)
    train: TrainSettings = Field(default_factory=TrainSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_file = Path(os.environ.get("WORKBENCH_CONFIG", str(DEFAULT_CONFIG_FILE)))
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file)
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
