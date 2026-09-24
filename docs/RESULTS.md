# RESULTS — 论文报告值（非本仓库测量）

> 本文件由 `make results`（`workbench results render`）从 `results/registry.yaml` 自动生成，请勿手改。
> **论文报告值，由官方模型在官方评测 harness 上测得，不是本仓库应用层的测量结果。**
> 本仓库的应用层（智能体、网关、记忆、服务）从未做过任何基准评测。

- 论文：arXiv 2602.10090 — Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning
- 核对的论文版本：未核对（PDF 未能访问）

| 基准 | 指标 | 模型 | 数值 | 来源（版本 / 表 / 行 / 列） | 状态 |
|---|---|---|---|---|---|
| BFCLv3 | Overall | 4B base | 54.92 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| BFCLv3 | Overall | 4B AWM | 64.50 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| BFCLv3 | Overall | 8B base | 53.83 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| BFCLv3 | Overall | 8B AWM | 65.94 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| BFCLv3 | Overall | 14B base | 61.25 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| BFCLv3 | Overall | 14B AWM | 70.18 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| tau2-bench | Pass@1 | 14B AWM | 39.03 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| MCP-Universe | success rate | 8B base | 6.70 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |
| MCP-Universe | success rate | 8B AWM | 11.17 | arXiv 2602.10090: ? / ? / ? / ? | **待核对** |

## 尚未录入的格子

- tau2-bench Pass@1：4B base, 4B AWM, 8B base, 8B AWM, 14B base；per-domain values (domains to be taken from the paper table)
- MCP-Universe success rate：4B base, 4B AWM, 14B base, 14B AWM；per-category values (categories to be taken from the paper table)
- BFCLv3 sub-category columns, if reported：—；only Overall is transcribed so far

`待核对`：数值转录自任务书 §1.4，尚未对照论文 PDF 的表格逐格核实（本开发环境无法访问 arxiv.org）。
