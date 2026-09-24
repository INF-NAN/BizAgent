# UPSTREAM — 上游关系与许可证（初稿，Phase 0）

> 本文件回答两个问题：
> 1. 仓库里哪些文件属于上游，哪些是本仓库新增（R5，精确到文件级）；
> 2. 各上游的许可证是什么，是否允许"公开 GitHub 仓库 + 个人作品展示"这一用途（R6）。
>
> 事实来源与行号见 `docs/RECON.md`。本文件随每个 Phase 更新。

## 1. 上游清单与固定版本

| 上游 | 本仓库中的位置 | 固定 SHA（提交日期） | 状态 |
|---|---|---|---|
| Snowflake-Labs/agent-world-model（AWM） | `third_party/agent-world-model`（git submodule，`shallow = true`） | `85e322f69279e3b3325b7377ec3bab788514e9cb`（2026-05-28） | 已初始化；未改动 |
| └ Raibows/mcp-adapted-bench（AWM 嵌套的评测 harness） | `third_party/agent-world-model/mcp-adapted-bench`（AWM 内的 gitlink） | `2dff8bdfc35082e5b6980c4954b6074159a15854` | **不初始化**（R1：禁止评测） |
| Agent-One-Lab/AgentFly | `third_party/AgentFly`（git submodule，`shallow = true`） | `1256586b1109ba8e0dc0f179f8515b4567d09df4`（2026-05-06） | 已初始化；未改动 |
| └ Agent-One-Lab/verl（AgentFly 嵌套的 veRL fork） | `third_party/AgentFly/verl`（AgentFly 内的 gitlink，SSH URL） | `001f000ae2e4cf05bb94c01427898cbe68961141`（2026-05-05） | 暂不初始化（Phase 7 需要时再初始化） |
| meta-pytorch/OpenEnv | 未纳入 | 侦察所用版本：`e401886d23aab1be92493ea15e5d7e2cdf7e657b`（2026-09-23） | 仅作参考，RECON 中以永久链接引用（理由见 RECON §3.4） |
| HF 数据集 `Snowflake/AgentWorldModel-1K` | 不入库；将来由下载脚本放到 `data/awm1k/`（gitignored） | — | 本环境无法访问 `huggingface.co` |
| HF 模型 `Snowflake/Arctic-AWM-4B/8B/14B` | 不入库；由 vLLM 在运行时拉取 | — | 本环境无法访问 `huggingface.co` |

补丁：`patches/` 目前为空，没有对任何上游文件做过改动。

核对命令：

```bash
git submodule status
git -C third_party/agent-world-model status --porcelain   # 应为空
git -C third_party/AgentFly status --porcelain            # 应为空
```

## 2. 文件级边界：上游 vs 本仓库（截至 Phase 0）

| 路径 | 归属 | 说明 |
|---|---|---|
| `third_party/agent-world-model/**` | 上游（AWM） | 只是 gitlink，本仓库不包含其文件副本；禁止直接修改（R5） |
| `third_party/AgentFly/**` | 上游（AgentFly） | 同上 |
| `.gitmodules` | 本仓库 | 子模块声明（URL、路径、`shallow = true`） |
| `TASK.md` | 本仓库 | 任务书原文 |
| `CLAUDE.md` | 本仓库 | R1–R14 要点摘要 |
| `docs/RECON.md` | 本仓库 | Phase 0 侦察报告 |
| `docs/UPSTREAM.md` | 本仓库 | 本文件 |
| `results/registry.yaml` | 本仓库 | 论文数字登记草稿（全部 `verified: false`）；数值来自论文，登记与结构由本仓库维护 |

后续各 Phase 新增的文件（`src/workbench/**`、`configs/**`、`scripts/**`、`tests/**`、`ui/**` 等）都会补登到本表。

## 3. 许可证调查（R6）

| 对象 | 许可证 | 证据 | 核实状态 |
|---|---|---|---|
| AWM 仓库代码 | **无许可证** | 4 个提交的完整历史中没有 LICENSE / COPYING / NOTICE（`git log --all --diff-filter=AD -- 'LICENSE*' 'COPYING*' 'NOTICE*'` 结果为空）；`pyproject.toml` 没有 `license` 字段；README 没有许可证声明。远端有一个未合并的 PR #17，是第三方贡献者提议加入 MIT LICENSE（2026-09-16），**不构成维护者授权** | 已核实（缺失） |
| AgentWorldModel-1K（HF 数据集） | **未能确认** | `huggingface.co` 被本环境出口策略拒绝。旁证：OpenEnv 示例 `examples/echo_on_agent_world_model/fixtures/SOURCE.md:9`（@e401886）写有 "CC-BY-4.0"，但这是第三方陈述 | 未核实 |
| Arctic-AWM-4B / 8B / 14B（HF 模型卡） | **未能确认** | 同上。WebSearch 摘要里没有给出其许可证；摘要对"与其它 Arctic 模型相同"的推测不采信 | 未核实 |
| AgentFly | Apache-2.0 | `third_party/AgentFly/LICENSE`（Apache License 2.0 全文）；`third_party/AgentFly/pyproject.toml:31` | 已核实 |
| veRL（AgentFly 嵌套 fork） | Apache-2.0 | fork @001f000 的 `LICENSE`、`setup.py:85`（`license="Apache 2.0"`）；`Notice.txt`：Copyright 2023-2024 Bytedance Ltd. and/or its affiliates | 已核实 |
| OpenEnv | BSD-3-Clause | @e401886 的 `LICENSE:1-3`（版权方 Hugging Face, Inc.）；`pyproject.toml:10` | 已核实（未纳入本仓库） |
| mcp-adapted-bench（不使用） | 无许可证文件 | 根目录没有 LICENSE，`pyproject.toml` 也没有 license 字段 | 已核实（缺失；按 R1 不使用） |

作为 PyPI 依赖使用、不随本仓库分发的第三方包，许可证取自 PyPI JSON 元数据：

| 包 | 许可证 |
|---|---|
| fastapi-mcp 0.4.0、mcp 1.26.0 | MIT |
| mcp-agent 0.2.6、vLLM 0.19.0 | Apache-2.0 |
| langgraph、langgraph-checkpoint-sqlite、langchain-core、langchain-mcp-adapters、pydantic-settings、typer | MIT |

### 3.1 与"公开 GitHub 仓库 + 个人作品展示"用途的关系

- **AWM（阻断项）**：代码没有任何许可证，默认保留全部权利。
  - 本仓库只含 gitlink（指向其公开仓库的指针），不包含 AWM 代码副本；
  - 但后续阶段会在运行时导入、执行 AWM 代码，并可能以 patch 形式修改它；
  - 这些行为在没有许可证的情况下缺乏明确授权。
  - 按 R6，**在仓库主人做出决定之前停止依赖 AWM 代码的后续工作**。
- **数据集与模型（阻断项）**：许可证未能核实。按 R6 同样需要确认后再继续。
- **AgentFly / veRL（Apache-2.0）、OpenEnv（BSD-3-Clause）**：允许公开展示和使用，分发副本时需要保留版权与许可证声明。本仓库只以 submodule 或链接方式引用，不分发副本。

## 4. 数据与权重

- 数据集与模型权重**不提交进仓库**（R6），只提供下载脚本（Phase 1 的 `make data`）。
- 自合成的环境只放在 `data/synth/`，manifest 标记 `origin: local-synth`，与官方数据隔离（R2，Phase 7）。
