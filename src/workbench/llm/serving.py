"""Build the `vllm serve` command from a serving profile (configs/serving/*.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ServingProfile:
    model: str
    host: str = "127.0.0.1"
    port: int = 8000
    served_model_name: str | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    enable_auto_tool_choice: bool = False
    tool_call_parser: str | None = None
    reasoning_parser: str | None = None
    chat_template: str | None = None
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> ServingProfile:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown serving keys: {sorted(unknown)}")
        return cls(**raw)


def vllm_command(p: ServingProfile) -> list[str]:
    if p.enable_auto_tool_choice and not p.tool_call_parser:
        # mirrors vLLM's own check (vllm/entrypoints/openai/cli_args.py:364)
        raise ValueError("enable_auto_tool_choice requires tool_call_parser")
    cmd = ["vllm", "serve", p.model, "--host", p.host, "--port", str(p.port)]
    if p.served_model_name:
        cmd += ["--served-model-name", p.served_model_name]
    if p.max_model_len is not None:
        cmd += ["--max-model-len", str(p.max_model_len)]
    if p.gpu_memory_utilization is not None:
        cmd += ["--gpu-memory-utilization", str(p.gpu_memory_utilization)]
    if p.enable_auto_tool_choice:
        cmd += ["--enable-auto-tool-choice", "--tool-call-parser", str(p.tool_call_parser)]
    if p.reasoning_parser:
        cmd += ["--reasoning-parser", p.reasoning_parser]
    if p.chat_template:
        cmd += ["--chat-template", p.chat_template]
    return cmd + list(p.extra_args)
