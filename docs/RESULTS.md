# RESULTS — 论文报告值（非本仓库测量）

> 本文件由 `make results`（`workbench results render`）从 `results/registry.yaml` 自动生成，请勿手改。
> **论文报告值，由官方模型在官方评测 harness 上测得，不是本仓库应用层的测量结果。**
> 本仓库的应用层（智能体、网关、记忆、服务）从未做过任何基准评测。

- 论文：arXiv 2602.10090 — Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning
- 核对的论文版本：v3（2026-05-22）
- 发表状态：Accepted to ICML 2026 (arXiv comments field of the abstract page)
- 核对记录：`docs/verification/2026-09-24-paper-table4.md`

## 表注

- Table 4 标题：Results across BFCLv3, τ²-bench, and MCP-Universe benchmarks. Best performance is in bold.
- BFCLv3 的 Hall. 指 hallucination 类别；数值为 BFCLv3 leaderboard 分数。
- τ²-bench 的 Pass@k 指允许 k 次尝试的任务成功率；Pass@1 为 4 次运行的平均值。
- MCP-Universe 报告任务成功率；论文排除了需要 GUI 的 3D design 任务，以及需要登录鉴权（GitHub、Notion）的 repository management 任务（§5.1）。
- Base 行为未经额外训练的 Qwen3 thinking 模型（§5.1）；本表只登记 Base 与 AWM 两行，未登记 Simulator 与 EnvScaler 两个对比基线。

## 登记的数值

| 基准 | 指标 | 单位 | 模型 | 数值 | 来源（版本 / 表 / 行 / 列） | 状态 |
|---|---|---|---|---|---|---|
| BFCLv3 | Overall | score | 4B Base | 54.92 | arXiv 2602.10090: v3 / 4 / 4B / Base / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Overall | score | 4B AWM | 64.50 | arXiv 2602.10090: v3 / 4 / 4B / AWM / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Overall | score | 8B Base | 53.83 | arXiv 2602.10090: v3 / 4 / 8B / Base / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Overall | score | 8B AWM | 65.94 | arXiv 2602.10090: v3 / 4 / 8B / AWM / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Overall | score | 14B Base | 61.25 | arXiv 2602.10090: v3 / 4 / 14B / Base / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Overall | score | 14B AWM | 70.18 | arXiv 2602.10090: v3 / 4 / 14B / AWM / BFCLv3 Leaderboard / Overall | 已核对 |
| BFCLv3 | Hall. | score | 4B Base | 73.93 | arXiv 2602.10090: v3 / 4 / 4B / Base / BFCLv3 Leaderboard / Hall. | 已核对 |
| BFCLv3 | Hall. | score | 4B AWM | 73.70 | arXiv 2602.10090: v3 / 4 / 4B / AWM / BFCLv3 Leaderboard / Hall. | 已核对 |
| BFCLv3 | Hall. | score | 8B Base | 76.42 | arXiv 2602.10090: v3 / 4 / 8B / Base / BFCLv3 Leaderboard / Hall. | 已核对 |
| BFCLv3 | Hall. | score | 8B AWM | 70.80 | arXiv 2602.10090: v3 / 4 / 8B / AWM / BFCLv3 Leaderboard / Hall. | 已核对 |
| BFCLv3 | Hall. | score | 14B Base | 77.94 | arXiv 2602.10090: v3 / 4 / 14B / Base / BFCLv3 Leaderboard / Hall. | 已核对 |
| BFCLv3 | Hall. | score | 14B AWM | 78.37 | arXiv 2602.10090: v3 / 4 / 14B / AWM / BFCLv3 Leaderboard / Hall. | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 4B Base | 15.83 | arXiv 2602.10090: v3 / 4 / 4B / Base / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 4B AWM | 22.57 | arXiv 2602.10090: v3 / 4 / 4B / AWM / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 8B Base | 26.44 | arXiv 2602.10090: v3 / 4 / 8B / Base / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 8B AWM | 33.45 | arXiv 2602.10090: v3 / 4 / 8B / AWM / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 14B Base | 36.69 | arXiv 2602.10090: v3 / 4 / 14B / Base / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@1 | pass rate (%) | 14B AWM | 39.03 | arXiv 2602.10090: v3 / 4 / 14B / AWM / tau2-bench / Overall / Pass@1 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 4B Base | 34.89 | arXiv 2602.10090: v3 / 4 / 4B / Base / tau2-bench / Overall / Pass@4 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 4B AWM | 43.89 | arXiv 2602.10090: v3 / 4 / 4B / AWM / tau2-bench / Overall / Pass@4 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 8B Base | 50.72 | arXiv 2602.10090: v3 / 4 / 8B / Base / tau2-bench / Overall / Pass@4 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 8B AWM | 55.40 | arXiv 2602.10090: v3 / 4 / 8B / AWM / tau2-bench / Overall / Pass@4 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 14B Base | 55.40 | arXiv 2602.10090: v3 / 4 / 14B / Base / tau2-bench / Overall / Pass@4 | 已核对 |
| tau2-bench | Pass@4 | pass rate (%) | 14B AWM | 57.19 | arXiv 2602.10090: v3 / 4 / 14B / AWM / tau2-bench / Overall / Pass@4 | 已核对 |
| MCP-Universe | Overall | success rate (%) | 4B Base | 6.15 | arXiv 2602.10090: v3 / 4 / 4B / Base / MCP-Universe / Overall | 已核对 |
| MCP-Universe | Overall | success rate (%) | 4B AWM | 6.70 | arXiv 2602.10090: v3 / 4 / 4B / AWM / MCP-Universe / Overall | 已核对 |
| MCP-Universe | Overall | success rate (%) | 8B Base | 6.70 | arXiv 2602.10090: v3 / 4 / 8B / Base / MCP-Universe / Overall | 已核对 |
| MCP-Universe | Overall | success rate (%) | 8B AWM | 11.17 | arXiv 2602.10090: v3 / 4 / 8B / AWM / MCP-Universe / Overall | 已核对 |
| MCP-Universe | Overall | success rate (%) | 14B Base | 8.38 | arXiv 2602.10090: v3 / 4 / 14B / Base / MCP-Universe / Overall | 已核对 |
| MCP-Universe | Overall | success rate (%) | 14B AWM | 12.29 | arXiv 2602.10090: v3 / 4 / 14B / AWM / MCP-Universe / Overall | 已核对 |

## 模型身份（推定）

| 行 | 推定身份与证据 | 身份已确认 |
|---|---|---|
| 4B Base (`4b.base`) | Qwen3-4B thinking model without additional training (paper §5.1: Base = the original LLMs ... without additional training; Qwen3 thinking models across 4B, 8B, 14B as base agents) | 否（推定） |
| 4B AWM (`4b.awm`) | Snowflake/Arctic-AWM-4B (HF sha 437dfa0). Card: apache-2.0, base_model Qwen/Qwen3-4B, "trained with agentic reinforcement learning on Qwen3-4B"; config Qwen3ForCausalLM, max_position_embeddings 40960. The card links the paper but does not say this checkpoint is the Table 4 AWM row. | 否（推定） |
| 8B Base (`8b.base`) | Qwen3-8B thinking model without additional training (paper §5.1: Base = the original LLMs ... without additional training; Qwen3 thinking models across 4B, 8B, 14B as base agents) | 否（推定） |
| 8B AWM (`8b.awm`) | Snowflake/Arctic-AWM-8B (HF sha 63ebcb9). Card: apache-2.0; text says "trained ... on Qwen3-8B" but the card metadata lists base_model Qwen/Qwen3-4B (inconsistent; config dims hidden_size 4096 / 36 layers match an 8B Qwen3); max_position_embeddings 40960. No statement that it is the Table 4 AWM row. | 否（推定） |
| 14B Base (`14b.base`) | Qwen3-14B thinking model without additional training (paper §5.1: Base = the original LLMs ... without additional training; Qwen3 thinking models across 4B, 8B, 14B as base agents) | 否（推定） |
| 14B AWM (`14b.awm`) | Snowflake/Arctic-AWM-14B (HF sha fa3e3b1). Card: apache-2.0; text says "trained ... on Qwen3-14B" but the card metadata lists base_model Qwen/Qwen3-4B (inconsistent; config dims hidden_size 5120 / 40 layers match a 14B Qwen3); max_position_embeddings 40960. No statement that it is the Table 4 AWM row. | 否（推定） |
