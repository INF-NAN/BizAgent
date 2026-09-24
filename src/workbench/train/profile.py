"""Training profiles. Only the `smoke` profile exists and it is validated against rule R2."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_MODEL_B = 1.7
MAX_STEPS = 5


class ProfileError(ValueError):
    """The profile violates rule R2 or is malformed."""


@dataclass(frozen=True)
class TrainProfile:
    profile: str
    model: str
    model_size_b: float
    train_files: Path
    val_files: Path
    no_results: bool
    overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> TrainProfile:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            p = cls(
                profile=str(raw["profile"]),
                model=str(raw["model"]),
                model_size_b=float(raw["model_size_b"]),
                train_files=Path(raw["train_files"]),
                val_files=Path(raw["val_files"]),
                no_results=bool(raw["no_results"]),
                overrides=dict(raw.get("overrides") or {}),
            )
        except KeyError as exc:
            raise ProfileError(f"missing key {exc} in {path}") from exc
        p.validate()
        return p

    def validate(self) -> None:
        errors = []
        if self.profile != "smoke":
            errors.append("only the 'smoke' profile may be launched (no full RL training, rule R2)")
        if self.model_size_b > MAX_MODEL_B:
            errors.append(f"model_size_b {self.model_size_b} > {MAX_MODEL_B}")
        if int(self.overrides.get("actor_rollout_ref.model.lora_rank", 0)) <= 0:
            errors.append("LoRA required: actor_rollout_ref.model.lora_rank must be > 0")
        steps = int(self.overrides.get("trainer.total_training_steps", 10**9))
        if steps > MAX_STEPS:
            errors.append(f"trainer.total_training_steps {steps} > {MAX_STEPS}")
        if not self.no_results:
            errors.append("no_results must be true")
        if errors:
            raise ProfileError("; ".join(errors))
