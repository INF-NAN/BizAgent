# configs/train

- `smoke.yaml` — the only training profile, validated by `workbench.train.profile`: ≤1.7B model,
  LoRA, ≤5 steps. The run directory it produces is marked `NO_RESULTS`.
- `smoke_data.json` — 8 hand-written arithmetic questions, used only so that AgentFly's built-in
  `calculator` tool and `math_equal_reward_tool` have something to act on. This is not
  evaluation data.
- No `paper_mirror.yaml`: only environment adapters are public; there is no complete official AWM
  training recipe to mirror (docs/UPSTREAM.md §7.3, ADR-012).
