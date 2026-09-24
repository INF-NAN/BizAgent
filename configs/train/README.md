# configs/train

- `smoke.yaml` — the only training profile (rule R2). ≤1.7B model, LoRA, ≤5 steps. The run
  directory it produces is marked `NO_RESULTS`.
- `smoke_data.json` — 8 hand-written arithmetic questions, used only so that AgentFly's built-in
  `calculator` tool and `math_equal_reward_tool` have something to act on. This is not
  evaluation data.
- No `paper_mirror.yaml`: Phase 0 concluded (b) — only environment adapters are public, there is
  no complete official AWM training recipe to mirror (docs/RECON.md §4, docs/LIMITATIONS.md).
