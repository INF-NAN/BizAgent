# UPSTREAM — 上游关系与许可证

> 本文件回答两个问题：
> 1. 仓库里哪些文件属于上游，哪些是本仓库新增（R5，精确到文件级）；
> 2. 各上游的许可证是什么，是否允许"公开 GitHub 仓库 + 个人作品展示"这一用途（R6）。
>
> 事实来源与行号见 `docs/RECON.md`。

## 1. 上游清单与固定版本

| 上游 | 本仓库中的位置 | 固定 SHA（提交日期） | 状态 |
|---|---|---|---|
| Snowflake-Labs/agent-world-model（AWM） | `third_party/agent-world-model`（git submodule，`shallow = true`） | `85e322f69279e3b3325b7377ec3bab788514e9cb`（2026-05-28） | 已初始化；未改动 |
| └ Raibows/mcp-adapted-bench（AWM 嵌套的评测 harness） | `third_party/agent-world-model/mcp-adapted-bench`（AWM 内的 gitlink） | `2dff8bdfc35082e5b6980c4954b6074159a15854` | **不初始化**（R1：禁止评测） |
| Agent-One-Lab/AgentFly | `third_party/AgentFly`（git submodule，`shallow = true`） | `1256586b1109ba8e0dc0f179f8515b4567d09df4`（2026-05-06） | 已初始化；未改动 |
| └ Agent-One-Lab/verl（AgentFly 嵌套的 veRL fork） | `third_party/AgentFly/verl`（AgentFly 内的 gitlink，SSH URL） | `001f000ae2e4cf05bb94c01427898cbe68961141`（2026-05-05） | 暂不初始化（Phase 7 需要时再初始化） |
| meta-pytorch/OpenEnv | 未纳入 | 侦察所用版本：`e401886d23aab1be92493ea15e5d7e2cdf7e657b`（2026-09-23） | 仅作参考，RECON 中以永久链接引用（理由见 RECON §3.4） |
| HF 数据集 `Snowflake/AgentWorldModel-1K` | 不入库；由 `make data` 下载到 `data/awm1k/`（gitignored） | HF 仓库最新提交（下载时记录于 `data/awm1k/MANIFEST.json`） | CC-BY-4.0，已于 2026-09-24 核实 |
| HF 模型 `Snowflake/Arctic-AWM-4B/8B/14B` | 不入库；由 vLLM 在运行时拉取 | 4B `437dfa0`、8B `63ebcb9`、14B `fa3e3b1`（2026-09-24 读取的 HF 提交） | Apache-2.0，已于 2026-09-24 核实 |

补丁：`patches/` 目前为空，没有对任何上游文件做过改动。

核对命令：

```bash
git submodule status
git -C third_party/agent-world-model status --porcelain   # 应为空
git -C third_party/AgentFly status --porcelain            # 应为空
```

## 2. 文件级边界：上游 vs 本仓库（Phase 8 定稿）

原则（R5）：上游只以 gitlink 形式出现在 `third_party/` 下；本仓库不包含任何上游文件的副本，`patches/` 为空（没有修改过任何上游文件）。下表之外的路径都不在版本库中（`data/`、`.cache/`、`.env` 等已被 `.gitignore` 排除）。

### 2.1 上游（只读引用）

| 路径 | 归属 | 本仓库如何使用 |
|---|---|---|
| `third_party/agent-world-model/**` | 上游 AWM @85e322f | app 环境以 editable path 依赖安装；运行时 import `awm.core.*`（建库、样例数据、校验），并以子进程启动其 MCP server 与 `awm gen` / `awm env` CLI。调用点与行号见 `docs/RECON.md` |
| `third_party/agent-world-model/mcp-adapted-bench` | 上游评测 harness | **不初始化，不使用**（R1） |
| `third_party/AgentFly/**` | 上游 AgentFly @1256586 | 只被 `train/` 环境以 path 依赖锁定；app 环境不 import（R11） |
| `third_party/AgentFly/verl` | 上游 veRL fork @001f000 | 默认不初始化；只在侦察时于 scratch 中只读检索，训练 preflight 会检查其是否已初始化 |

### 2.2 本仓库新增

| 路径 | 内容 |
|---|---|
| `.gitmodules` | 子模块声明（URL、路径、`shallow = true`） |
| `LICENSE` | 本仓库自有代码的 MIT 许可证（版权人 Yueyi Li）；不覆盖上游、数据集与模型（§3.2） |
| `TASK.md`、`TASK_v2.md`、`CLAUDE.md` | 任务书原文（Phase 0–8、Phase 9–15）；R1–R14 要点与当前状态 |
| `docs/verification/**` | Phase 9 起的外部核实记录与真实环境运行日志（N1） |
| `README.md` | 项目说明（英文摘要 + 中文正文） |
| `docs/RECON.md` | 上游侦察报告，记录全部"文件:行号" |
| `docs/UPSTREAM.md` | 本文件 |
| `docs/DECISIONS.md` | ADR-001 至 ADR-027 |
| `docs/ARCHITECTURE.md` | 分层图与审批写操作时序图 |
| `docs/WALKTHROUGH.md` | 学习路线 |
| `docs/LIMITATIONS.md` | 未验证项与已知限制 |
| `docs/RESULTS.md` | 由 registry 自动生成，勿手改 |
| `docs/CHANGELOG.md` | prompt 版本变更记录 |
| `docs/IDEAS.md` | 范围外想法（R13） |
| `docs/runbooks/phase15-gpu.md` | Phase 15 GPU runbook：15A 推理链路、15B smoke 训练、回贴与脱敏要求（仓库主人在自己的 GPU 机器上执行，UNVERIFIED-LOCAL） |
| `results/registry.yaml` | 论文数字登记（数值来自论文，结构由本仓库维护） |
| `pyproject.toml`、`uv.lock` | app 环境 |
| `train/pyproject.toml`、`train/uv.lock` | train 环境（只锁定，不在 CI 安装）；flash-attn 的构建环境使用锁定的 torch（ADR-027） |
| `Makefile` | 开发入口 |
| `.pre-commit-config.yaml`、`.secrets.baseline` | ruff、detect-secrets、submodule 干净检查 |
| `.github/workflows/ci.yml` | CI（lint、test、check-numbers） |
| `.github/workflows/docker-smoke.yml` | Docker 冒烟：构建、启动（不带 gpu profile）、经 HTTP API 走一遍 mock 演示、停止；不推送镜像 |
| `.gitignore`、`.dockerignore`、`.env.example` | 忽略规则与环境变量模板（无密钥） |
| `Dockerfile`、`docker-compose.yml` | 部署（env-manager、app、可选 vllm）。镜像内含 AWM 代码，只用于本地和 CI 构建，不得推送到任何镜像仓库（§3.1、ADR-003） |
| `configs/app.yaml` | 应用默认配置 |
| `configs/tool_policy.yaml` | 网关风险分级、审批、限流策略 |
| `configs/serving/arctic-awm-4b.yaml` | vLLM 服务 profile |
| `configs/pricing.yaml` | 合成账本的价格表（DeepSeek 官方价格页，上界口径） |
| `configs/number_whitelist.yaml` | 数字守卫白名单 |
| `configs/train/{smoke.yaml,smoke_data.json,README.md}` | smoke 训练 profile 与数据 |
| `scripts/download_data.sh` | 数据集下载脚本（UNVERIFIED-LOCAL） |
| `scripts/serve_vllm.sh` | vLLM 启动脚本（UNVERIFIED-LOCAL） |
| `scripts/demo_ui_check.py` | 浏览器端 demo 自检（Playwright） |
| `scripts/docker_smoke.py` | Docker 冒烟脚本（只用标准库；测量镜像大小与冷启动耗时） |
| `scripts/redact_paste.py` | Phase 15 回贴内容的脱敏脚本（N1；邮箱、卡号、电话沿用网关审计规则） |
| `src/workbench/{__init__,cli,config,doctor,runtime}.py` | CLI、配置、自检、运行时装配 |
| `src/workbench/subprocess_env.py` | 子进程环境变量白名单（ADR-019）与训练环境白名单（ADR-026） |
| `src/workbench/verify.py` | `workbench verify`：不给 key 地运行 `awm verify`，sql 模式的裁判经本地代理（ADR-025） |
| `src/workbench/envs/*.py` | 环境管理：端口、快照与 diff、健康检查、场景目录、AWM 适配、进程组管理、env-manager 服务 |
| `src/workbench/gateway/*.py` | MCP 网关：策略、限流、审计、错误归一、上游连接、核心、server |
| `src/workbench/llm/**` | LLM 客户端：类型、错误、`<tool_call>` 解析、mock replay 与 OpenAI 兼容后端、vLLM 命令生成；`workbench serve probe` 的两个探针（U1） |
| `src/workbench/agent/**` | LangGraph 智能体：状态、守卫、记忆、prompt（版本化）、节点、图、runner |
| `src/workbench/api/*.py` | HTTP API、SSE、schema |
| `src/workbench/obs/*.py` | trace（JSONL）与 Prometheus 指标 |
| `src/workbench/synth/*.py` | 合成编排：步骤计划、checkpoint、LLM 代理（缓存、重试、账本、预算熔断）、中断回收、校验报告 |
| `src/workbench/train/*.py` | 训练启动器：profile（R2 约束）、preflight、launch |
| `src/workbench/results/*.py` | registry 加载与 RESULTS.md 生成；数字守卫 |
| `ui/{index.html,app.js,style.css}` | 静态 UI（无构建步骤） |
| `tests/unit/**`、`tests/integration/**`、`tests/conftest.py` | 测试 |
| `tests/fixtures/awm_mini/**` | 手写迷你电商场景（按 AWM 数据格式编写，非官方数据）。7 个工具的名称、参数名、必填字段和顶层返回字段已与官方 `e_commerce_33` 对账并据此修正（CC-BY-4.0 来源与改动说明见其 `MANIFEST.json` 与 `docs/verification/2026-09-24-fixture-reconciliation.md`）；代码与数据均为手写，未复制官方内容 |
| `tests/fixtures/trajectories/*.jsonl` | 手写 mock LLM 脚本（非模型输出） |

## 3. 许可证调查（R6）

| 对象 | 许可证 | 证据 | 核实状态 |
|---|---|---|---|
| AWM 仓库代码 | **无许可证** | 2026-09-24 复查：上游 `HEAD` 仍为 `85e322f`，历史中没有 LICENSE / COPYING / NOTICE；`pyproject.toml` 没有 `license` 字段。第三方贡献者的 PR #17（提议 MIT）仍为 Open、未获批准，**不构成维护者授权**（`docs/verification/2026-09-24-licenses.md`） | 已核实（缺失） |
| AgentWorldModel-1K（HF 数据集） | **CC-BY-4.0** | 数据集卡 front matter `license: "cc-by-4.0"`（`docs/verification/2026-09-24-licenses.md`） | 已核实（2026-09-24） |
| Arctic-AWM-4B / 8B / 14B（HF 模型卡） | **Apache-2.0**（三者相同） | 模型卡 front matter `license: apache-2.0`；基座 Qwen3 同为 Apache-2.0（`docs/verification/2026-09-24-model-cards.md`） | 已核实（2026-09-24） |
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

- **AWM（阻断项，已按 ADR-003 处理）**：代码没有任何许可证，默认保留全部权利。
  - 仓库主人未就此答复，按 ADR-003 采用最保守做法：只含 gitlink（指向其公开仓库的指针），不包含 AWM 代码副本，也不做任何 patch；
  - 运行时由使用者自行从 AWM 公开仓库获取代码并在本地执行；README 显著位置写明 AWM 无许可证；
  - **Docker 镜像**（仓库主人决定 D20，2026-09-25）：
    - `Dockerfile` 把 `third_party/agent-world-model` 复制进镜像（`COPY third_party/agent-world-model third_party/agent-world-model`），所以构建出的镜像内含 AWM 代码；
    - 由于 AWM 目前没有许可证，**镜像只用于本地和 CI 构建，不得推送到任何公开或私有的镜像仓库**；
    - 2026-09-25 检查：`.github/workflows/` 下只有 `ci.yml` 与 `docker-smoke.yml`，都没有登录镜像仓库或推送镜像的步骤，`push` 只作为 git 触发条件出现；仓库其它受版本控制的文件中也没有 `docker push`、`docker compose push`、`docker login` 之类的命令；
    - docker-smoke 在 GitHub 托管 runner 上构建的镜像随 runner 回收，不上传。
  - 如果 AWM 上游将来加入许可证，需要重新评估本节。
- **数据集（CC-BY-4.0）与模型（Apache-2.0）**：均允许公开展示与使用。本仓库不包含数据与权重，只提供下载脚本；使用数据集须按 CC-BY-4.0 署名（见 §5）。开发与 CI 测试基于手写的迷你夹具，不依赖官方数据。
- **AgentFly / veRL（Apache-2.0）、OpenEnv（BSD-3-Clause）**：允许公开展示和使用，分发副本时需要保留版权与许可证声明。本仓库只以 submodule 或链接方式引用，不分发副本。

### 3.2 本仓库自有代码的许可证

- 2026-09-25（Phase 16）起，本仓库自有代码按 MIT 授权：根目录 `LICENSE`，版权人 Yueyi Li；`pyproject.toml` 与 `train/pyproject.toml` 的 `license = "MIT"`。
- MIT 只覆盖 §2.2 所列的本仓库文件。§2.1 的上游、数据集与模型各按 §3 表中的条款；AWM 仍没有许可证，本仓库不对其代码授予任何权利。
- 仓库中摘自 AgentWorldModel-1K 的内容仍按 CC-BY-4.0 署名（§5），包括 `docs/verification/` 中的运行记录与工具清单，以及迷你夹具借用的接口命名。

## 4. 数据与权重

- 数据集与模型权重**不提交进仓库**（R6），只提供下载脚本（Phase 1 的 `make data`）。
- 自合成的环境只放在 `data/synth/`，manifest 标记 `origin: local-synth`，与官方数据隔离（R2，Phase 7）。

## 5. 署名（CC-BY-4.0）

本仓库使用的 AgentWorldModel-1K 数据集按 CC-BY-4.0 授权，署名如下：

- **作品**：AgentWorldModel-1K（数据集）
- **作者**：Zhaoyang Wang, Canwen Xu, Boyi Liu, Yite Wang, Siwei Han, Zhewei Yao, Huaxiu Yao, Yuxiong He（UNC-Chapel Hill 与 Snowflake AI Research）
- **相关论文**：*Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning*，arXiv:2602.10090
- **链接**：https://huggingface.co/datasets/Snowflake/AgentWorldModel-1K
- **许可证**：Creative Commons Attribution 4.0 International（CC-BY-4.0），https://creativecommons.org/licenses/by/4.0/
- **改动说明**：本仓库**不分发**该数据集，也不修改它；`make data` 从 Hugging Face 原样下载固定 revision 到本地（`data/awm1k/`，不入库），运行时只读。本仓库只收录了从官方 `e_commerce_33` 整理出的工具名与参数名清单（`docs/verification/e_commerce_33-tools.md`），以及测试夹具中借用的接口命名（来源与改动见 `tests/fixtures/awm_mini/MANIFEST.json`）。

