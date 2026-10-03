# UPSTREAM — 上游关系、接口与许可证

本文件说明三件事：哪些文件属于上游、哪些属于本仓库；各上游的许可证与本仓库的使用方式；本仓库依赖的上游接口与行为，附"文件:行号"。行号对应第 1 节固定的版本；子模块内的路径相对子模块根目录，未纳入子模块的上游用固定 SHA 的永久链接。

## 1. 上游清单与固定版本

| 上游 | 本仓库中的位置 | 固定版本 | 用途 |
|---|---|---|---|
| Snowflake-Labs/agent-world-model（AWM） | `third_party/agent-world-model`，git submodule，`shallow = true` | `85e322f69279e3b3325b7377ec3bab788514e9cb` | 环境建库、MCP server、合成 CLI、`awm agent` / `awm verify` |
| Raibows/mcp-adapted-bench（AWM 嵌套的评测 harness） | AWM 内的 gitlink `mcp-adapted-bench` | `2dff8bdfc35082e5b6980c4954b6074159a15854` | 不初始化，不使用 |
| Agent-One-Lab/AgentFly | `third_party/AgentFly`，git submodule，`shallow = true` | `1256586b1109ba8e0dc0f179f8515b4567d09df4` | 只在独立的 train 环境中使用 |
| Agent-One-Lab/verl（AgentFly 嵌套的 veRL fork） | AgentFly 内的 gitlink `verl`，SSH 地址 | `001f000ae2e4cf05bb94c01427898cbe68961141` | 默认不初始化；smoke 训练前按 [DEPLOYMENT.md](DEPLOYMENT.md) §4 初始化 |
| meta-pytorch/OpenEnv | 未纳入 | 参考版本 `e401886d23aab1be92493ea15e5d7e2cdf7e657b` | 对照 AWM 环境适配的实现，文中以永久链接引用 |
| HF 数据集 `Snowflake/AgentWorldModel-1K` | `make data` 下载到 `data/awm1k/`，不入库 | 本仓库使用 revision `dde80a0283fe781bdc51656bce57063dc5650213`，下载时写入 `data/awm1k/MANIFEST.json` | 官方场景 |
| HF 模型 `Snowflake/Arctic-AWM-4B/8B/14B` | 不入库，由 vLLM 在运行时下载 | 4B `437dfa0`、8B `63ebcb9`、14B `fa3e3b1` | 模型服务 |
| HF 模型 `Qwen/Qwen3-4B-Instruct-2507` | 不入库，`scripts/lab/run_all.sh` 下载到 `data/models/` | 按名称下载，不固定修订 | 批量实验的基座与学生模型（Apache-2.0） |
| DeepSeek API | 外部服务 | 模型名由 `TEACHER_MODEL` 指定，默认 `deepseek-flash` | 批量实验的教师 |

`patches/` 不存在：本仓库没有修改过任何上游文件。核对方式：

```bash
git submodule status
git -C third_party/agent-world-model status --porcelain   # 应为空
git -C third_party/AgentFly status --porcelain            # 应为空
```

pre-commit 的 `no-upstream-edits` 钩子在每次提交时做同样的检查。所有命令都设置 `PYTHONPYCACHEPREFIX`（Makefile、CI、`tests/conftest.py`），运行 AWM 时 Python 不会在子模块里留下 `__pycache__`。

## 2. 文件级边界

上游只以 gitlink 形式出现在 `third_party/` 下，本仓库不包含任何上游文件的副本。下表之外的路径不在版本库中（`data/`、`.cache/`、`.env` 等由 `.gitignore` 排除）。

### 2.1 上游（只读引用）

| 路径 | 本仓库如何使用 |
|---|---|
| `third_party/agent-world-model/**` | app 环境以 editable path 依赖安装；运行时 import `awm.core.*`（建库、样例数据、校验），以子进程启动其 MCP server 与 `awm gen` / `awm env` / `awm verify` CLI（第 6 节） |
| `third_party/agent-world-model/mcp-adapted-bench` | 不初始化，不使用 |
| `third_party/AgentFly/**` | 只被 `train/` 环境以 path 依赖锁定；app 环境不 import |
| `third_party/AgentFly/verl` | 默认不初始化；训练 preflight 检查它是否已初始化 |

### 2.2 本仓库

| 路径 | 内容 |
|---|---|
| `.gitmodules` | 子模块声明（URL、路径、`shallow = true`） |
| `LICENSE` | 本仓库自有代码的 MIT 许可证（第 3.2 节） |
| `README.md` | 项目说明 |
| `docs/ARCHITECTURE.md` | 分层图、审批写操作时序图、智能体状态机、prompt 版本 |
| `docs/DECISIONS.md` | 架构决策记录 ADR-001 至 ADR-028 |
| `docs/UPSTREAM.md` | 本文件 |
| `docs/WALKTHROUGH.md` | 学习路线 |
| `docs/DEPLOYMENT.md` | GPU 模型服务与 smoke 训练的部署指南 |
| `docs/EXPERIMENTS.md` | 批量实验的设计、运行方式与局限（ADR-028） |
| `docs/RESULTS.md` | 由 `results/registry.yaml` 生成，勿手改 |
| `docs/examples/*.md` | 官方 `e_commerce_33` 的工具清单与一次 DeepSeek 端到端运行的记录（CC-BY-4.0 摘录，第 5 节） |
| `docs/assets/*.png` | README 的 mock 演示截图，由 `scripts/demo_ui_check.py` 生成 |
| `results/registry.yaml` | 论文数字登记（数值来自论文，结构由本仓库维护） |
| `results/lab/` | 批量实验一次完整运行生成的报告与 `summary.json` |
| `pyproject.toml`、`uv.lock`、`.python-version` | app 环境 |
| `train/pyproject.toml`、`train/uv.lock` | train 环境（只锁定，CI 不安装）；flash-attn 的构建环境使用锁定的 torch（ADR-025） |
| `Makefile` | 开发入口 |
| `.pre-commit-config.yaml`、`.secrets.baseline` | ruff、detect-secrets、子模块干净检查 |
| `.github/workflows/ci.yml` | CI：lint（含相对链接检查）、secrets、test、check-numbers、doctor |
| `.github/workflows/docker-smoke.yml` | Docker 冒烟：构建、启动（不带 `gpu` profile）、经 HTTP API 走一遍 mock 演示、停止；不推送镜像 |
| `.gitignore`、`.dockerignore`、`.env.example` | 忽略规则与环境变量模板（无密钥） |
| `Dockerfile`、`docker-compose.yml` | 部署：env-manager、app，可选 vllm（`gpu` profile）。镜像内含 AWM 代码，只在本地和 CI 构建（第 3.1 节） |
| `configs/app.yaml` | 应用默认配置 |
| `configs/tool_policy.yaml` | 网关的允许清单、风险分级、审批级别与限流 |
| `configs/approval_policy.yaml` | 默认审批策略：不自动批准任何调用（ADR-027） |
| `configs/approval_policy.demo.yaml` | 演示审批策略：三种决策各一例，供 `make demo-mock`、docker-smoke 与测试使用 |
| `configs/serving/arctic-awm-4b.yaml` | vLLM 服务 profile |
| `configs/pricing.yaml` | 合成账本的价格表（上界口径） |
| `configs/number_whitelist.yaml` | 数字守卫的白名单（工程数字，逐条写明理由） |
| `configs/train/` | smoke 训练 profile、数据与说明 |
| `configs/lab/` | 批量实验的审批策略：自动批准 write（`eval_policy.yaml`）、另外拒绝全部 destructive（`deny_destructive.yaml`） |
| `scripts/download_data.sh` | 数据集下载 |
| `scripts/serve_vllm.sh` | 按 serving profile 启动 vLLM |
| `scripts/demo_ui_check.py` | 浏览器端 demo 自检（Playwright），生成 README 截图 |
| `scripts/docker_smoke.py` | Docker 冒烟（只用标准库；输出镜像大小与冷启动耗时） |
| `scripts/measure_preview.py` | 审批前预演各阶段耗时的测量脚本 |
| `scripts/check_links.py` | README 与 `docs/**/*.md` 的相对链接与图片检查 |
| `scripts/redact_log.py` | 分享日志前脱敏（token、URL 凭据、邮箱、卡号、电话） |
| `scripts/lab/` | 批量实验：一键脚本 `run_all.sh`、GPU 环境依赖、LoRA 训练、失败预测模型 |
| `src/workbench/{__init__,cli,config,doctor,runtime}.py` | CLI、配置、自检、运行时装配 |
| `src/workbench/subprocess_env.py` | 子进程环境变量白名单（ADR-017）与训练环境白名单（ADR-024） |
| `src/workbench/verify.py` | `workbench verify`：不把 key 交给 `awm verify`（ADR-023） |
| `src/workbench/envs/` | 环境管理：端口、快照与 diff、行级改动与结构比对、健康检查、场景目录、AWM 适配、进程组、审批前预演的影子环境、env-manager 服务 |
| `src/workbench/gateway/` | MCP 网关：风险分级、审批策略、限流、审计、错误归一、上游连接、核心（预演记录、令牌、执行后比对）、MCP server |
| `src/workbench/llm/` | LLM 客户端：类型、错误、`<tool_call>` 解析、mock replay 与 OpenAI 兼容后端、`reasoning_content` 回传、vLLM 命令生成与探针 |
| `src/workbench/agent/` | LangGraph 智能体：状态、守卫、记忆、版本化 prompt、节点、图、runner |
| `src/workbench/api/` | HTTP API、SSE、schema |
| `src/workbench/obs/` | trace（JSONL）与 Prometheus 指标 |
| `src/workbench/synth/` | 合成编排：步骤计划、checkpoint、LLM 代理（缓存、重试、账本、预算熔断）、中断回收、校验报告 |
| `src/workbench/train/` | 训练启动器：profile 约束、preflight、launch |
| `src/workbench/results/` | registry 加载、RESULTS.md 生成、数字守卫 |
| `src/workbench/lab/` | 批量实验：场景级划分、episode 运行与验证、LLM 调用录制、注入、SFT 数据、推理服务回放、统计与报告 |
| `ui/` | 静态 UI（无构建步骤） |
| `tests/unit/`、`tests/integration/`、`tests/conftest.py` | 测试 |
| `tests/fixtures/awm_mini/` | 手写的迷你电商场景 `mini_e_commerce`，按 AWM 数据格式编写。7 个工具的名称、参数名、必填字段与顶层返回字段与官方 `e_commerce_33` 一致；代码、表、样例数据、任务与 verifier 都是手写的，没有复制官方内容（来源说明见其 `MANIFEST.json`） |
| `tests/fixtures/trajectories/*.jsonl` | 手写的 mock LLM 脚本，不是模型输出 |

## 3. 许可证

| 对象 | 许可证 | 依据 |
|---|---|---|
| AWM 仓库代码 | 无 | 固定提交与上游 `main` 的历史中都没有 LICENSE、COPYING 或 NOTICE；`pyproject.toml` 没有 `license` 字段。第三方贡献者提议加入 MIT 的 PR #17 未合并，不构成维护者授权 |
| AgentWorldModel-1K 数据集 | CC-BY-4.0 | 数据集卡 front matter `license: "cc-by-4.0"` |
| Arctic-AWM-4B / 8B / 14B | Apache-2.0 | 模型卡 front matter `license: apache-2.0`；基座 Qwen3 同为 Apache-2.0 |
| AgentFly | Apache-2.0 | `third_party/AgentFly/LICENSE`；`third_party/AgentFly/pyproject.toml:31` |
| veRL fork | Apache-2.0 | fork 的 `LICENSE` 与 [setup.py#L85](https://github.com/Agent-One-Lab/verl/blob/001f000ae2e4cf05bb94c01427898cbe68961141/setup.py#L85)；`Notice.txt`：Copyright 2023-2024 Bytedance Ltd. and/or its affiliates |
| OpenEnv（未纳入） | BSD-3-Clause | [LICENSE#L1-L3](https://github.com/meta-pytorch/OpenEnv/blob/e401886d23aab1be92493ea15e5d7e2cdf7e657b/LICENSE#L1-L3)，版权方 Hugging Face, Inc. |
| mcp-adapted-bench（不使用） | 无许可证文件 | 根目录没有 LICENSE，`pyproject.toml` 没有 `license` 字段 |

作为 PyPI 依赖安装、不随本仓库分发的包，许可证取自 PyPI 元数据：fastapi-mcp 0.4.0、mcp 1.26.0 为 MIT；mcp-agent 0.2.6、vLLM 0.19.0 为 Apache-2.0；langgraph、langgraph-checkpoint-sqlite、langchain-core、langchain-mcp-adapters、pydantic-settings、typer 为 MIT。

### 3.1 使用方式与限制

- AWM 没有许可证，默认保留全部权利。本仓库只包含指向其公开仓库的 gitlink，不包含 AWM 代码副本，也不打补丁；使用者从 AWM 的公开仓库取得代码并在本地运行。本仓库不对 AWM 代码授予任何权利，使用前请自行判断（ADR-003）。AWM 将来加入许可证时，需要重新评估本节。
- Docker 镜像：`Dockerfile` 执行 `COPY third_party/agent-world-model third_party/agent-world-model`，镜像因此内含 AWM 代码。镜像只在本地和 CI 中构建，不得推送到任何公开或私有的镜像仓库。仓库中的工作流与脚本都没有登录镜像仓库或推送镜像的步骤；docker-smoke 在 GitHub 托管 runner 上构建的镜像随 runner 回收。
- 数据集（CC-BY-4.0）与模型（Apache-2.0）允许公开展示与使用。本仓库不包含数据与权重，只提供下载方式；使用数据集须按第 5 节署名。开发与 CI 测试使用手写的迷你夹具，不依赖官方数据。
- AgentFly、veRL（Apache-2.0）与 OpenEnv（BSD-3-Clause）允许公开展示和使用，分发副本时须保留版权与许可证声明。本仓库只以 submodule 或链接引用它们，不分发副本。

### 3.2 本仓库自有代码

- 本仓库自有代码按 MIT 授权：根目录 `LICENSE`，版权人 Yueyi Li；`pyproject.toml` 与 `train/pyproject.toml` 的 `license = "MIT"`。
- MIT 只覆盖第 2.2 节所列的文件。第 2.1 节的上游、数据集与模型各按第 3 节表中的条款。
- 仓库中摘自 AgentWorldModel-1K 的内容按 CC-BY-4.0 署名（第 5 节）。

## 4. 数据与权重

- 数据集与模型权重不提交进仓库，只提供下载方式：`make data`（`scripts/download_data.sh`，可用 `AWM1K_REVISION` 固定 revision）与 vLLM 的运行时下载。
- 官方数据只读。自合成的环境只放在 `data/synth/<run>/`，manifest 标记 `origin: local-synth`，与官方数据隔离（ADR-011）。
- 批量实验（docs/EXPERIMENTS.md）下载基座模型 `Qwen/Qwen3-4B-Instruct-2507`（Apache-2.0）到 `data/models/`，训练出的 LoRA adapter 与全部运行记录留在 `data/lab/`，都不入库；只有生成的报告与 `summary.json` 提交在 `results/lab/`。教师轨迹来自 DeepSeek API 的输出；用这些输出训练模型之前，请自行确认 DeepSeek 服务条款中的相关规定。

## 5. 署名（CC-BY-4.0）

本仓库使用的 AgentWorldModel-1K 数据集按 CC-BY-4.0 授权：

- 作品：AgentWorldModel-1K（数据集）
- 作者：Zhaoyang Wang, Canwen Xu, Boyi Liu, Yite Wang, Siwei Han, Zhewei Yao, Huaxiu Yao, Yuxiong He（UNC-Chapel Hill 与 Snowflake AI Research）
- 相关论文：*Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning*，arXiv:2602.10090
- 链接：https://huggingface.co/datasets/Snowflake/AgentWorldModel-1K
- 许可证：Creative Commons Attribution 4.0 International（CC-BY-4.0），https://creativecommons.org/licenses/by/4.0/
- 改动说明：本仓库不分发、不修改该数据集；`make data` 从 Hugging Face 原样下载到本地 `data/awm1k/`，运行时只读。仓库中只收录了以下摘录：`docs/examples/e-commerce-33-tools.md` 中 `e_commerce_33` 的工具名与参数名；`docs/examples/e-commerce-33-deepseek.md` 中该场景任务 0 的任务文本、工具调用参数与少量数据行；迷你夹具借用的接口命名（来源与改动见 `tests/fixtures/awm_mini/MANIFEST.json`）。

## 6. AWM 接口要点

以下路径相对 `third_party/agent-world-model/`。

### 6.1 CLI 与合成用环境变量

- 控制台脚本 `awm = "awm.cli:main"`（`pyproject.toml:39-40`）；参数由 `simpleArgParser.parse_args_with_commands` 解析（`awm/cli.py:113-126`），解析后调用 dataclass 的 `pre_process()`。
- 命令枚举 `TopCmd`、`GenCmd`、`EnvCmd`（`awm/cli.py:6-46`），分发表 `DISPATCH`（`:94-110`）把每个命令映射到模块的 `run(config)`，参数由各模块的 `@dataclass Config` 定义：

| 命令 | 模块与 Config | 关键参数（默认值） |
|---|---|---|
| `gen scenario` | `awm/core/scenario.py:26-48` | `input_path`、`output_path`、`target_count=1000`（含种子在内的总数）、`resume=False` |
| `gen task` | `awm/core/task.py:11-19` | `input`（场景文件）、`output`、`num_tasks=10`、`limit=None` |
| `gen db` | `awm/core/db.py:10-18` | `input`、`output`、`database_dir` |
| `gen sample` | `awm/core/sample.py:11-20` | `input_task`、`input_db`、`output`、`database_dir` |
| `gen spec` | `awm/core/spec.py:10-15` | `input_task`、`input_db`、`output` |
| `gen env` | `awm/core/env.py:20-28` | `input_spec`、`input_db`、`output`、`database_dir`、`max_retry=4` |
| `gen verifier` | `awm/core/verifier.py:25-36` | `input_task`、`output`、`mode=sql`、`database_dir` |
| `env start` | `awm/core/server.py:13-26` | `scenario`、`envs_load_path`、`db_path`、`host`、`port`、`temp_server_path`、`output_dir` |
| `env check` | `awm/core/check.py:7-12` | `url`、`timeout=10.0` |
| `env check_all` | `awm/core/test_env.py:8-13` | `input`、`allowed_scenarios` |
| `env reset_db` | `awm/core/reset.py:11-16` | `input_db`、`input_sample`、`database_dir`、`scenarios` |
| `agent` | `awm/core/agent.py:49-80` | 第 6.4 节 |
| `verify` | `awm/core/verify.py:36-49` | 第 6.5 节 |

- `env check_all` 的参数名是 `input`（`awm/core/test_env.py:11`），AWM README 写的 `--output` 与源码不符，本仓库按源码调用。
- `bench` 依赖未初始化的 `mcp-adapted-bench`，导入失败时被登记为 `None`（`awm/cli.py:65-68,89`）；`uv sync` 与 `awm --help` 在它缺失时都正常。
- 合成用的环境变量：`AWM_SYN_LLM_PROVIDER`（`awm/gpt.py:38-40`）、`OPENAI_API_KEY` 与 `OPENAI_BASE_URL`（`awm/gpt.py:54-55`，`awm/tools.py:420-429`）、`AWM_SYN_OVERRIDE_MODEL`（`awm/gpt.py:42-46`，各 gen 步骤在 `pre_process` 中断言它已设置）、`EMBEDDING_OPENAI_API_KEY`（`gen scenario` 必需，`awm/core/scenario.py:63`）与 `EMBEDDING_OPENAI_BASE_URL`（`:64-65,85`）；另有 `AZURE_ENDPOINT_URL`、`AZURE_OPENAI_API_KEY`（`awm/gpt.py:51-52`）。
- `gen scenario` 去重使用 `text-embedding-3-large`（`awm/core/scenario.py:40-43`），没有 embedding 端点时无法运行；`gen task` 只读取场景文件每行的 `name` 与 `description`（`awm/core/task.py:44-45`），因此可以直接以手写的场景文件为输入（ADR-019）。
- `gen scenario` 会把分类结果写回输入的种子文件（`awm/core/scenario.py:642`），所以编排层先把种子复制进运行目录（ADR-011）。

### 6.2 MCP server：启动方式、传输与文件副作用

- 调用链：`awm env start` → `run`（`awm/core/server.py:198-199`）→ `run_server`（`:146-171`）。服务端代码取自 `gen_envs.jsonl` 中该场景的 `full_code`（`:97-101`；同名场景取最后一条，`:98-99`），然后：
  - 把 `create_engine(...)` 一行改为指向会话 SQLite 文件（`:105-109`）；
  - 在 `uvicorn.run(app` 之前注入 `FastApiMCP(app)` 与 `mcp.mount_http()`（`:111-128`）；
  - 入口按 `os.environ.get('HOST', …)`、`os.environ.get('PORT', …)` 取地址（`:112-116`）；`PORT` 与 `DATABASE_PATH` 由启动器写入自己的环境（`:157-158`）。
- 传输方式是 MCP Streamable HTTP：fastapi-mcp 0.4.0 的 `mount_http()` 默认挂在 `/mcp`，包装 MCP SDK 的 `StreamableHTTPSessionManager`（`fastapi_mcp/server.py:312,332,351`；`fastapi_mcp/transport/http.py:21,52-56`，`json_response=True`、`stateless=False`）。AWM 自己的客户端也以 `streamable_http` 连接（`awm/tools.py:159-162`）。
- 工具即 FastAPI 路由，工具名取 OpenAPI `operationId`（`fastapi_mcp/server.py:619`；没有 operationId 的操作被跳过，`fastapi_mcp/openapi/convert.py:50-63`）。生成 prompt 要求每个路由写 snake_case 的 `operation_id`（`awm/prompts.py:274`），官方 1000 个场景的 35062 个路由都是 `@app.<method>(..., operation_id="...")` 字面量。没有 `operation_id` 时，FastAPI 0.115.12 用 `generate_unique_id` 生成（`fastapi/utils.py:179-184`，OpenAPI 取值见 `fastapi/openapi/utils.py:237,248`）。
- 所有用户相关操作隐式使用 `user_id=1`（`awm/prompts.py:62-63,227`）。
- 进程模型：`run_server` 以 `os.system(f"{python} {code} 2>&1 | tee {log}")` 阻塞运行（`:163`），无法在进程内托管。只终止 `python -m awm.core.server` 这个启动器时，`sh -c … | tee` 与真正的服务进程都会存活；按进程组 `killpg` 才能全部回收（ADR-004）。
- 文件副作用：
  - 未指定 `--temp_server_path` 时，`temp_server_<scenario>.py` 写到 `envs_load_path` 所在目录（`:134-138`），指向官方数据目录时就会写进去；
  - 显式传入 `--db_path`、`--temp_server_path`、`--output_dir` 时，AWM 只写这三处：创建 `output_dir`（`:50`）、复制 `initial.db`（`:67-68`）、写服务代码（`:140`）与 `server_code.py`（`:152`）、`server.log`（`:161-163`），正常退出后再复制 `final.db`（`:166-168`）。本仓库每次启动都显式传这三个参数。
- 健康检查 `check_mcp_server` 用 streamable_http 连接并在超时内执行 `list_tools`，至少一个工具才算运行中（`awm/tools.py:141-199`）。
- `awm agent --scenario` 自动起服用 `start_server_process`（`:174-195`）：不传 `--temp_server_path`，`Popen` 不传 `env=`，结束时只终止父进程。

### 6.3 工具调用的返回形态与错误文本

- fastapi-mcp 经进程内 `httpx.AsyncClient(ASGITransport)` 调用路由，超时 10 秒（`fastapi_mcp/server.py:115-119`）；HTTP 4xx/5xx 变成 `Exception("Error calling {tool}. Status code: …")`（`:558-561`），MCP SDK 1.26.0 再包装为 `CallToolResult(isError=True)`（`mcp/server/lowlevel/server.py:467-472,583-584`）。
- MCP SDK 默认按 inputSchema 校验参数（`:492`），失败时返回 `Input validation error: …`（`:532`）。实际出现过的三种文本：`'bogus' is not one of ['price', 'rating']`、`'product_id' is a required property`、`'abc' is not of type 'integer'`。
- 生成的环境代码被禁止写错误处理（`awm/prompts.py:234`），运行期错误一律是 HTTP 500 → `isError`，文本形如 `Error calling get_product_by_id. Status code: 500. Response: Internal Server Error`。
- 查询没有结果时不报错。官方环境把列表包在对象里：`{"products": [], "total": 0}`、`{"cart_id": 1, "items": []}`、`{"payment_methods": [...]}`；删除类工具返回 `{"success": bool}`；写操作返回写入的对象，例如 `{"cart_item": {...}}`。`e_commerce_33` 的 39 个工具没有枚举参数，`sort_by` 是自由字符串（ADR-007）。
- 生成代码的语义缺陷：查询不存在的商品 ID 返回 `id: 0` 的占位对象，向购物车加入不存在的 offer ID 也会写入成功。网关只能判为 `ok`，这类问题靠审批与 DB diff 暴露（ADR-007、ADR-027）。

### 6.4 `awm agent`

- Config（`awm/core/agent.py:49-80`）：`max_iterations=30`、`temperature=1.0`、`max_tokens=2048`；`envs_path`、`tasks_path`、`db_path`、`sample_path` 默认在 `./outputs/` 下。
- 两种运行方式（`:395-458`）：`--task` 或 `--scenario` + `--task_id` 配合 `--mcp_url` 连接已有服务；只给 `--scenario` + `--task_id` 时在输出目录建库并随机端口起服。两者可以同时给：任务从 `--tasks_path` 查出，`--mcp_url` 存在时不起服、不准备数据库（`:398-407,433-458`）。
- 自动起服有三个上游缺陷：工作库直接建成 `<output_dir>/final.db`（`awm/core/server.py:70-82`），结束时又复制到同一路径（`awm/core/agent.py:596-598`），必然抛 `shutil.SameFileError`；服务代码写到 `--envs_path` 所在目录；只终止启动器，服务进程残留。本仓库只用 `--mcp_url` 模式连接 env-manager 启动的会话。
- System prompt（`:88-127`）只暴露 `list_tools` 与 `call_tool` 两个元函数，要求以 `<tool_call>{"name": …, "arguments": …}</tool_call>` 文本输出；解析用正则 `<tool_call>\s*(.*?)\s*</tool_call>`（`:130-167`，正则在 `:132`），兼容 `mcp_tool_` 前缀。某一步没有解析出工具调用即结束循环（`:506-518`），只执行第一个调用（`:520-528`）。
- 请求（`:339-386`）：只发 `model`、`messages`、`max_completion_tokens`、`temperature`，不带 `tools` 与 `tool_choice`，非流式。地址同时含 `localhost` 与 `v1` 时附加 vLLM 参数 `add_generation_prompt`、`min_tokens=16`、`chat_template_kwargs.enable_thinking=True`，并以 `tool` 角色回传工具结果（`:355-379,417`）；否则工具结果以 user 消息 `Tool response:` 回传。key 取 `OPENAI_API_KEY`，没有时为 `EMPTY`（`awm/tools.py:429`）。
- 工具执行器每次调用都新开一个 MCP session，超时 60 秒（`:273,299-336`）；`isError` 以 `"Error: …"` 字符串返回。
- 输出目录默认 `outputs/agents/<ts>[_<scenario>_task_<id>]`，可用 `--output_dir` 覆盖（`:419-427`）。`trajectory.json` 的顶层字段为 `scenario, task_id, task, model, api_url, max_iterations, temperature, total_iterations, timestamp, trajectory, messages`（`:578-592`）；`trajectory[]` 条目含 `iteration, role, content, tool_calls[], tool_response`，最后一条带 `is_final: true`（`:511-517,560-569`）。`--mcp_url` 模式不保存数据库。

### 6.5 `awm verify`

- Config（`awm/core/verify.py:36-49`）：`input`（运行目录）、`init_db_path`、`final_db_path`、`mode`（默认 `sql`）、`verifier_path`、`verifier_code_path`。默认 verifier 文件为 `./outputs/gen_verifier.jsonl`（sql）与 `./outputs/gen_verifier.pure_code.jsonl`（code，`:376-379`）；显式传入的数据库路径优先（`:367-368`）。
- `sql` 模式：`exec` 执行 verifier 代码得到信息字典（`:104-148`），再交给 LLM 裁判（`:230-334`），分类为 `complete / incomplete / server_error / agent_error`，解析失败为 `judge_error`；裁判的地址、key 与模型由 `resolve_llm_config` 从环境读取（`:416-433`，`awm/tools.py:386-437`），请求带 `temperature=1.0`、`max_completion_tokens=4096`（`:302-310`）。只有 sql 模式检查 LLM 环境变量（`:51-80`）。
- `code` 模式：执行纯代码 verifier，结果只有 `complete` 或 `others`（`:151-198`），不调用 LLM。
- 两种模式都以完整 `__builtins__` 在本进程中 `exec` 数据集里的代码，namespace 直接提供 `os`（`:104-126,151-174`）；执行期间把两个数据库设为只读，结束后恢复（`:111-117,158-164`）。本仓库因此经 `workbench verify` 运行它（ADR-023）。
- 两个 verifier 文件含重复的 `(scenario, task_idx)` 行，AWM 取第一条匹配（`find_scenario_entry`，`awm/tools.py:456-472`）。
- 结果写到 `<input>/verify.<mode>.json`（`:436-437`），字段为 `scenario, task_id, task, mode, reward_type, verify_result`，sql 模式另有 `llm_judge`。

### 6.6 数据集文件与字段

| 文件 | 写入位置 | 每行字段 |
|---|---|---|
| `gen_scenario.jsonl` | `awm/core/scenario.py:629` | `name, description` |
| `gen_tasks.jsonl` | `awm/core/task.py:142` | `scenario, tasks[]` |
| `gen_db.jsonl` | `awm/core/db.py:259` | `scenario, db_schema{tables[{name, ddl, indexes[], examples[]}]}, db_path` |
| `gen_sample.jsonl` | `awm/core/sample.py:282` | `scenario, tables_count, inserts_count, sample_data{tables[{insert_statements[]}]}` |
| `gen_spec.jsonl` | `awm/core/spec.py:120` | `scenario, api_spec{api_groups[{endpoints[]}]}` |
| `gen_envs.jsonl` | `awm/core/env.py:568` | `scenario, db_path, full_code` |
| `gen_verifier.jsonl`、`gen_verifier.pure_code.jsonl` | `awm/core/verifier.py:175` | `scenario, task_idx, task, verification{code, raw_response}` |

- revision `dde80a0` 有 1000 个场景，场景名全部是 `<类别>_<序号>`，其中也有以 `local_` 开头的（`local_search_1`、`local_services_marketplace_1`），所以手写场景名的约定是以 `local_` 开头且不以 `_<数字>` 结尾（ADR-019）。
- `check_all` 读取每条记录的 `db_path` 并复制该数据库（`awm/core/env.py:158`），没有数据库文件时先执行 `env reset_db`。

### 6.7 合成步骤：输出、续跑与 LLM 客户端的重试

- 写输出的方式：`gen scenario`、`gen task`、`gen db`、`gen sample`、`gen spec`、`gen env` 都在结束时一次覆盖写出（见第 6.6 节的行号）；`gen verifier` 每处理一批就追加写入（`awm/core/verifier.py:172-176,456`），续跑时只重新生成校验不通过的行并追加在后面（`:145-156,260-308`）。因为 `awm verify` 取第一条匹配的行，中途中断后直接重跑会让旧的无效行被选中（ADR-020）。
- `gen env` 自带续跑，只保留 `full_code` 长度大于 10 的已有结果（`awm/core/env.py:120-133,334-370`）；`gen sample` 插入样例前先重建数据库（`awm/core/sample.py:210-212`）。
- `gen env` 与 `check_all` 的测试 server 以 `start_new_session=True` 启动（`awm/core/env.py:161-172`），不在调用者的进程组里；正常路径上 AWM 用 `killpg` 回收（`:212-227`），临时目录 `/tmp/env_test_*`（`:148`）在 `finally` 中删除（`:230-235`）。AWM 没有注册信号处理或 `atexit`，被 SIGTERM 结束时这段 `finally` 不执行。
- LLM 客户端 `GPTClient`（`awm/gpt.py`）：非流式 `chat.completions.create`（`:171`），把 `max_tokens` 改名为 `max_completion_tokens`（`:160-164`）；超时 600 秒，并发上限 64，每个请求最多尝试 3 次，间隔 3 秒、6 秒（`:36,168-204`）；仍然失败时返回内容为空的 refusal completion（`:103-131,205-206`），步骤照常以 0 退出。因此本仓库按代理账本判定步骤（ADR-021、ADR-022）。
- 各 gen 步骤每次尝试一批请求，最多 5 次尝试（`max_retry=4`），失败后先用一次"错误摘要"请求总结错误再重试；主请求的 `max_tokens` 为 32000（task、verifier）或 128000（db、sample、spec、env）。
- openai SDK 2.38.0 把 402 映射为普通的 `APIStatusError`（`openai/_client.py:1081-1112`），自己只重试 408、409、429 与 5xx，默认 2 次（`openai/_base_client.py:795-826`，`openai/_constants.py:10`）。

### 6.8 子进程与环境变量

- `awm env check_all` 用 `subprocess.Popen([sys.executable, '-m', 'awm.core.server', ...], start_new_session=True)` 启动每个环境，不传 `env=`（`awm/core/env.py:161-172`）；`gen verifier` 在进程内 `exec` 生成的 verifier（`awm/core/verifier.py:104`）。
- AWM 自己的 MCP 客户端在连接时用 `isolated_mcp_env()` 临时删掉非白名单变量（`awm/tools.py:118-139`），只作用于客户端进程，不作用于 server。
- 启动器经 `awm.tools` → mcp-agent 0.2.6 import scikit-learn，后者设置 `KMP_DUPLICATE_LIB_OK`、`KMP_INIT_AT_FORK`（`sklearn/__init__.py:56,60`），所以 env server 的进程组里有这两个非密钥变量。
- 官方 1000 个场景的 `full_code` 只按字面名读取 `PORT`、`HOST`、`DATABASE_PATH`，没有整体访问 `os.environ`。本仓库在此基础上只给执行生成代码的子进程白名单变量（ADR-017）。

## 7. AgentFly 与 veRL

以下路径相对 `third_party/AgentFly/`。

### 7.1 入口、嵌套子模块与安装

- 没有 console script，入口为 `python -m agentfly.cli <train|deploy|swebench|search>`（`src/agentfly/cli.py:7-45`）；`train` 把参数交给 Hydra 脚本 `agentfly.verl.trainer.main_ppo`（`:23-30`）。
- `src/agentfly/verl` 是指向 `../../verl/verl` 的软链接（git mode 120000），即嵌套 submodule `verl`。它的 URL 是 SSH 形式 `git@github.com:Agent-One-Lab/verl.git`（`.gitmodules:3`）；没有 SSH key 时用 `git -c url."https://github.com/".insteadOf="git@github.com:" submodule update --init verl`，`-c` 经 `GIT_CONFIG_PARAMETERS` 传给克隆进程，不需要修改 `.gitmodules`。
- `requires-python = ">=3.12,<3.13"`（`pyproject.toml:30`）；依赖含 `vllm==0.19.0`（`:44`），`verl` extra 含 `flash-attn`（`:64`），其构建依赖写作 `[tool.uv.extra-build-dependencies] flash-attn = ["torch"]`（`:98-99`）。上游的 `src/agentfly/requirements.txt:9` 固定 `vllm==0.10.0`，与 pyproject 不一致。
- AgentFly 的 `.gitignore` 包含 `build/`、`dist/`、`*.egg-info/`、`__pycache__/`，editable 安装不会让子模块出现未跟踪文件。
- 工具与奖励在 import 时由装饰器注册（`src/agentfly/tools/decorator.py:52`，`src/agentfly/rewards/reward_base.py:298-341`），训练入口不会自动加载外部插件。smoke 配置使用内置的 `calculator`（`tools/src/calculate/tools.py:6-9`）与 `math_equal_reward_tool`（`rewards/math_reward.py:492-495`）。`calculator` 用 sympy 1.14.0 的 `sympify` 解析模型输出，而 `sympify` 内部使用 `eval`（`sympy/core/sympify.py:138-139`）。
- 训练栈读取的环境变量：AgentFly 读取 `XDG_CACHE_HOME`、`AGENT_DATA_DIR`、`AGENT_CONFIG_DIR`、`TOOL_ERROR_AS_OBSERVATION`（`agentfly/__init__.py:16-47`）、`REWARD_DECOMPOSITION*`（`agents/agent_base.py:155-171`）与 `AGENTFLY_RAY_GET_DEFAULT_TIMEOUT_SEC`；veRL 读取 `VERL_*`、`NCCL_*`、`CUDA_*`、`TORCH_*`、`CUBLAS_WORKSPACE_CONFIG`、`FLASH_ATTENTION_DETERMINISTIC` 与分布式变量，跟踪器的 key 只在启用对应 logger 时读取（ADR-024）。

### 7.2 Hydra 配置键、上游不一致与缺陷

- veRL fork @`001f000` 的版本为 `0.8.0.dev`。Hydra 入口 `@hydra.main(config_path="config", config_name="ppo_trainer")`（`verl/trainer/main_ppo.py:34`）；LoRA 键在 `actor_rollout_ref.model.lora_rank / lora_alpha / target_modules`；agent 段在 `agent.init_config.*` 与 `agent.run_config.{max_turns,num_chains,generation_config}`；trainer 段有 `nnodes`、`n_gpus_per_node`、`default_local_dir`。console logger 每个 step 打印一行 `step:N - key:value - …`（`verl/utils/logger/aggregate_logger.py:26-31`）。
- AgentFly 自带的 `examples/train_scripts/train_example.sh:57-103` 使用的 `agent.max_turns`、`agent.num_chains`、`agent.init_config.backend`、`agent.generation_config.max_tokens` 在它固定的 fork 配置中不存在。`configs/train/smoke.yaml` 跟随 fork 的实际结构，preflight 的 `config-keys` 检查会拦下不存在的键。
- fork 中残留未解决的合并冲突标记：`verl/trainer/config/_generated_ppo_trainer.yaml:177-185`、`_generated_ppo_megatron_trainer.yaml:64-83`、`verl/utils/checkpoint/megatron_checkpoint_manager.py:481-506,612-633`。前两个是 Hydra 不加载的展开参考文件，后者只影响 Megatron 路径（smoke 走 FSDP）；静态键检查容忍这些标记，权威检查是 train 环境里的 Hydra 组合。
- veRL fork 的 `install_requires` 含 `numpy<2.0.0`、`tensordict>=0.8.0,<=0.10.0,!=0.9.0`，vLLM extra 为 `vllm>=0.8.5,<=0.12.0`（[setup.py#L26-L52](https://github.com/Agent-One-Lab/verl/blob/001f000ae2e4cf05bb94c01427898cbe68961141/setup.py#L26-L52)）。AgentFly 以软链接使用 veRL 而不 pip 安装它，这些约束不会被强制执行。

### 7.3 训练配方的公开状态

检索范围包括 AgentFly 的 `main` 与另外 9 个分支、veRL fork 的 4 个分支、AWM 全仓与其评测 harness、OpenEnv 的 `envs/agent_world_model_env` 与 `examples/`。结论是只公开了环境适配，没有完整的训练配方：

- OpenEnv 把 AWM 环境包装成 WebSocket 会话服务，并有步级奖励映射（[awm_environment.py#L464-L470](https://github.com/meta-pytorch/OpenEnv/blob/e401886d23aab1be92493ea15e5d7e2cdf7e657b/envs/agent_world_model_env/server/awm_environment.py#L464-L470)），但没有 trainer 配置、超参数或启动脚本；
- AWM 本体没有训练代码，评测 harness 中只有一段 `<think>` 格式规则（[rule_reward.py#L4-L30](https://github.com/Raibows/mcp-adapted-bench/blob/2dff8bdfc35082e5b6980c4954b6074159a15854/mcp_adapted_bench/common/rule_reward.py#L4-L30)）；
- 各处都没有 GRPO 超参数、滑动窗口历史的实现、训练子集清单或启动脚本。

因此本仓库不创建 `paper_mirror` profile，smoke 训练只演示 AgentFly 自己的"rollout → 奖励 → 更新"链路（ADR-012）。

## 8. 依赖约束

| 组件 | 关键约束 | 来源 |
|---|---|---|
| AWM | Python `>=3.12`；`fastapi==0.115.12`、`fastapi-mcp==0.4.0`、`mcp-agent==0.2.6`、`sqlalchemy==2.0.41`、`numpy>=2.4.2`；lock：`mcp 1.26.0`、`starlette 0.46.2`、`pydantic 2.12.5` | `third_party/agent-world-model/pyproject.toml:6-20`、`uv.lock` |
| AgentFly | Python `>=3.12,<3.13`；`vllm==0.19.0`；`[verl]` 含 `flash-attn`、`peft`、`ray[default]` | `third_party/AgentFly/pyproject.toml:30,44,58-80` |
| veRL fork | `numpy<2.0.0`、`tensordict>=0.8.0,<=0.10.0,!=0.9.0`、`ray[default]>=2.41.0` | 第 7.2 节 |
| vLLM 0.19.0 | `torch==2.10.0`、`transformers>=4.56.0,<5`、`mcp`（不限版本） | PyPI 元数据 |
| langchain-mcp-adapters 0.3.2 | `mcp>=1.24.0,<2.0.0` | PyPI 元数据 |
| sse-starlette | 3.0.3 与 starlette 0.46.2 兼容；更新的版本要求 `starlette>=0.49.1` | PyPI 元数据、AWM `uv.lock` |

- numpy 的约束在 AWM（`>=2.4.2`）与 veRL（`<2.0.0`）之间无解，torch 与 vLLM 也不应进入 app 环境，所以拆成 app 与 train 两个 uv 环境（ADR-002）。
- AWM 固定 `fastapi==0.115.12`，把 starlette 锁在 0.46.x，API 层的 SSE 因此用 sse-starlette 3.0.3。
- app 环境用 `constraint-dependencies` 把关键包锁在 AWM `uv.lock` 的版本上：`mcp 1.26.0`、`pydantic 2.12.5`、`pydantic-settings 2.12.0`、`starlette 0.46.2`、`sse-starlette 3.0.3`、`uvicorn 0.40.0`、`httpx 0.28.1`、`openai 2.38.0`、`numpy 2.4.2`、`anyio 4.12.1`、`typer 0.21.1`。
- train 环境按 AgentFly 解析，关键版本为 `vllm 0.19.0`、`torch 2.10.0`、`transformers 4.57.6`、`numpy 2.2.6`、`ray 2.58.0`、`tensordict 0.14.2`、`flash-attn 2.8.3.post1`、`peft 0.21.0`；解析出的 `numpy` 与 `tensordict` 超出 veRL `setup.py` 的约束，这是上游自身的不一致，本仓库不修正。
- uv 0.8.17 的 `extra-build-dependencies`：写成 `"torch"` 时构建环境安装 PyPI 上最新的 torch；写成 `{ requirement = "torch", match-runtime = true }` 时使用锁文件中的版本。uv 只读取项目根目录的 `[tool.uv]`，所以这个设置写在 `train/pyproject.toml` 即可，不需要改 AgentFly（ADR-025）。
- `uv sync --frozen` 严格按锁文件记录的地址（files.pythonhosted.org）下载，PyPI 镜像设置对它不起作用；改用 `--locked` 会因锁文件记录的是 pypi.org 而要求重新锁定。

## 9. 模型服务：Arctic-AWM 与 vLLM

- 模型卡：三个模型都是 `Qwen3ForCausalLM`，`max_position_embeddings` 40960。4B 的 `base_model` 为 Qwen/Qwen3-4B；8B、14B 卡片正文写基座为 Qwen3-8B、Qwen3-14B，但元数据的 `base_model` 仍是 Qwen/Qwen3-4B，与正文和 `config.json` 的维度不符。卡片链接了论文，但没有说明发布的权重就是论文 Table 4 中 AWM 行评测的模型，`results/registry.yaml` 因此把模型身份记为推定。
- `chat_template.jinja`（三个模型字节相同，md5 `da05f6b8a81932c7cf5f26eb545d4417`）：带 `tools` 时 system prompt 要求输出 `<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>`（第 1-11 行），历史中的工具调用按同一格式渲染（第 57-75 行），工具结果以 `<tool_response>` 包裹；支持 `enable_thinking`。`tokenizer_config.json` 中 `<tool_call>`、`</tool_call>`（151657、151658）与 `<think>`、`</think>` 都是 `special: false`，默认解码不会删掉它们。
- vLLM v0.19.0（tag commit `2a69949`）：
  - `gpu_memory_utilization` 默认 0.9（`vllm/config/cache.py:41`），`max_model_len` 默认由模型配置推导（`vllm/config/model.py:182`）；
  - `hermes` tool parser 注册在 `vllm/tool_parsers/__init__.py:61-64`，按 `<tool_call>(.*?)</tool_call>|<tool_call>(.*)` 取出标签内的 JSON（`vllm/tool_parsers/hermes_tool_parser.py:61-66,89-139`），流式按同样的标签切分（`:160-208`）；与模板格式一致；
  - `--enable-auto-tool-choice` 必须配合 `--tool-call-parser`（`vllm/entrypoints/openai/cli_args.py:363-364`）；
  - 请求带 `tools` 却没有 `tool_choice` 时默认为 `"auto"`（`vllm/entrypoints/openai/chat_completion/protocol.py:640-643`），未开启自动工具选择时返回错误 `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set`（`vllm/entrypoints/serve/render/serving.py:197-215`）；
  - 不带 `tools` 的请求 `tool_choice` 保持默认 `"none"`（`chat_completion/protocol.py:175-181`），预处理不调用 parser（`serve/render/serving.py:534-550`），非流式原样返回 `content`（`chat_completion/serving.py:1476-1479`），流式要求 `request.tools` 非空才创建 parser（`:532-535,1743-1757`）。`awm agent` 的请求属于这一类，它的 `<tool_call>` 文本留在 `content` 中由 AWM 自己解析；
  - 在 CUDA 上使用自带的 `vllm.vllm_flash_attn`，外部 `flash_attn` 包只在 ROCm 上使用（`vllm/v1/attention/backends/fa_utils.py:18-44`）；
  - 使用统计可用 `VLLM_NO_USAGE_STATS=1` 或 `DO_NOT_TRACK=1` 关闭（`vllm/usage/usage_lib.py:52-66`）。
- `qwen3` reasoning parser 已注册（`vllm/reasoning/__init__.py:83`）；serving profile 不设置它，思考内容留在 `content` 中，由 workbench 客户端去掉（ADR-018）。

## 10. 其它依赖的行为依据

### 10.1 LangGraph

LangGraph 1.2.12，langgraph-checkpoint-sqlite 3.1.1。

- `interrupt(value)`（`langgraph/types.py:880-887`）与 `Command`（`:827`）。恢复被 `interrupt` 暂停的节点时，整个节点从头重新执行，所以 approve 节点在 `interrupt` 之前不做任何有副作用的事，"请求审批"事件在 act 节点发出，预演放在单独的 preview 节点（ADR-026）。
- `AsyncSqliteSaver`（`langgraph/checkpoint/sqlite/aio.py:38`）；`SqliteStore(conn, ttl=TTLConfig)`（`langgraph/store/sqlite/base.py:855-864`），`TTLConfig.default_ttl` 的单位是分钟（`langgraph/store/base/__init__.py:545`）。`sweep_ttl` 把带微秒的 `expires_at` 与只到秒的 `CURRENT_TIMESTAMP` 按字符串比较（`base.py:1139`），过期要到下一秒才会被清扫。

### 10.2 MCP SDK、Starlette、sse-starlette

- MCP SDK 1.26.0 客户端：`streamablehttp_client(url, headers, timeout, sse_read_timeout, ...)`（`mcp/client/streamable_http.py:686-693`），`ClientSession.initialize / call_tool / list_tools`（`mcp/client/session.py:148,368,505`）。
- 服务端：lowlevel `Server.call_tool(*, validate_input=True)`（`mcp/server/lowlevel/server.py:492`），处理器可以直接返回 `CallToolResult`（`:539-540`），请求上下文中的 `request` 就是 HTTP 请求对象（`:758`）；`StreamableHTTPSessionManager(app, event_store, json_response=False, stateless=False, ...)`（`mcp/server/streamable_http_manager.py:60-65`）。网关用 `validate_input=False`，让参数错误也经过自己的归一（ADR-007）。
- Starlette 的 `Mount("/mcp")` 访问 `/mcp` 时 307 重定向，MCP 客户端不跟随，所以网关用 `Route` 挂一个 ASGI 类实例；Starlette 不运行被 mount 的子应用的 lifespan，网关的 `StreamableHTTPSessionManager.run()` 由 API 的 lifespan 进入。
- sse-starlette 3.0.3：客户端断开时 `_listen_for_disconnect` 取消整个任务组（`sse_starlette/sse.py:194-202,262-275`），取消信号经流式生成器传到 LangGraph 的 `astream`，正在进行的 LLM 调用随之取消。
- uvicorn 0.40.0 停止时先停止接收新连接，再等待在途请求完成（`uvicorn/server.py:265-281`），`timeout_graceful_shutdown` 默认不限时（`uvicorn/config.py:217`）。

### 10.3 SQLite 与审批前预演的依据

- `PRAGMA table_info` 每列返回 `(cid, name, type, notnull, dflt_value, pk)`；`workbench.envs.changes` 用 `type` 与 `dflt_value` 识别时间列，用 `pk` 取主键。
- 类型恰为 `INTEGER` 的单列主键是 rowid 的别名，未指定时由 SQLite 按"当前最大值加一"分配（带 AUTOINCREMENT 时按 `sqlite_sequence`）；从同一个数据库出发的两次插入得到同一个值，所以按值比对。
- `sqlite3.Connection.backup` 在另一个连接持有数据库时也能得到一致的副本；快照、预演与 checkpoint 都用它复制会话数据库。
- 对官方数据集 revision `dde80a0` 的统计（1000 个场景、18465 张表）是时间列与主键规则的依据：
  - 主键：INTEGER rowid 别名 17248 张，复合主键 636 张，其它单列主键 570 张，无主键 11 张；非 INTEGER 单列主键的表，新增行只比对行数；
  - 生成的 server 代码中 `datetime.utcnow()` 出现在 989 个场景里，时间列在调用时写入，两次执行必然不同，所以只记录、不比对；`uuid4(`、`secrets.token_`、`random.` 用于生成 token、slug 或带前缀的 ID，可能作为 TEXT 主键；
  - `httpx.` 只在 1 个场景中出现，`requests.`、`subprocess.`、`os.system(` 都没有出现：预演再执行一次生成的代码，几乎没有数据库之外的副作用（ADR-026）。

### 10.4 pydantic、fnmatch 与 YAML（审批策略）

pydantic 2.12.5，PyYAML 6.0.3，Python 3.12。

- `ConfigDict(extra="forbid")`（`pydantic/config.py:63`，默认 `'ignore'`）让未声明的字段报 `extra_forbidden`；`ValidationError.errors()` 的每一项带 `type`、`loc` 与 `msg`，`workbench.gateway.approval_policy` 据此拼出 `rules[0] (id x).args.quantity.lte` 这样的 YAML 路径。
- 数值字段用 `BeforeValidator`（`pydantic/functional_validators.py:91`）显式检查类型，`True` 或 `"5"` 不会被悄悄转成数字；其中抛出的 `ValueError` 以 "Value error, " 开头，加载器去掉这个前缀。
- `fnmatch.fnmatchcase` 区分大小写，模式被转换成以 `\Z` 结尾的正则并从头匹配，所以是整名匹配：`e_commerce_*` 不匹配 `mini_e_commerce`，`*` 可以匹配 `_`。
- `yaml.safe_load` 读取策略文件；空文件得到 `None`，按 `{}` 校验，报 `version: Field required`。
