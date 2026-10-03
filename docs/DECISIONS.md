# DECISIONS — 架构决策记录（ADR）

本文件记录影响本仓库结构、边界与默认值的设计决定，每条写明背景、决定与后果。编号连续，文中以编号互相引用。

---

## ADR-001 上游以 git submodule 引用

- 背景：本仓库在 AWM 与 AgentFly 之上做应用层工程，不修改上游代码，也不改变被评测的模型与数据。AWM 没有发布到 PyPI，也没有许可证（ADR-003）。
- 决定：
  - `third_party/` 下的上游都是 git submodule，固定到 commit SHA，并设 `shallow = true`；本仓库自有代码全部在 `src/workbench/`；
  - 确需改动上游时，只能以 `patches/NNNN-<desc>.patch` 加一条 ADR 的方式进行，`patches/` 为空；
  - 不把上游代码拷进 `src/`：那样要维护一份分叉，对没有许可证的 AWM 也不能这样做。
- 后果：
  - 克隆后要执行 `git submodule update --init`，`workbench doctor` 会检查；
  - 运行 AWM 时 Python 会往子模块里写 `__pycache__`，因此 Makefile、CI 与测试的 conftest 统一设置 `PYTHONPYCACHEPREFIX`；
  - 升级上游要显式修改 SHA，并重新核对 docs/UPSTREAM.md 中的源码引用。

## ADR-002 app 与 train 两个独立的 uv 环境

- 背景：AWM 要求 `numpy>=2.4.2`，AgentFly 的 veRL fork 要求 `numpy<2.0.0`；vLLM v0.19.0 绑定 `torch==2.10.0`（docs/UPSTREAM.md §8）。在一个环境里取折中版本，等于改动上游固定的版本。
- 决定：
  - 根目录是 app 环境，用 `constraint-dependencies` 把关键包锁在 AWM `uv.lock` 的版本上；
  - `train/` 是独立环境，完全按 AgentFly 的固定版本解析；开发机与 CI 只检查它的 lock（`uv lock --check`），在 GPU 机器上按 docs/DEPLOYMENT.md 安装；
  - app 只以子进程方式调用 train（`workbench train ...`），app 代码不 import torch 分布式、veRL 或 AgentFly 的训练模块。
- 后果：两套 lock 分别维护；app 与 train 之间只能通过文件和子进程交互。

## ADR-003 AWM 没有许可证时的使用方式

- 背景：AWM 仓库在固定的 `85e322f` 上没有 LICENSE、COPYING 或 NOTICE 文件，`pyproject.toml` 也没有 `license` 字段（docs/UPSTREAM.md §3）。没有许可证就没有得到复制、修改或分发的授权。
- 决定：
  - 只以 submodule 指针引用 AWM，在运行时调用；不复制、不分发、不打补丁，本仓库不含任何 AWM 代码副本，`patches/` 保持为空；
  - README 与 docs/UPSTREAM.md 显著声明：AWM 未声明许可证，本仓库不对其代码授予任何权利；
  - 手写的迷你夹具只借用 AWM 的数据格式，不含 AWM 代码；
  - `Dockerfile` 构建时把 `third_party/agent-world-model` 复制进镜像，镜像因此内含 AWM 代码。镜像只在本地和 CI 中构建，不得推送到任何公开或私有的镜像仓库，工作流中也没有推送镜像的步骤；
  - 没有改用 OpenEnv（BSD-3）的环境实现：它同样依赖 AWM 生成的数据，问题并没有消失。
- 后果：
  - 以指针引用并在运行时调用的法律边界并不明确，使用者需要自行判断；
  - 镜像不能发布，部署方只能从源码自行构建；
  - AWM 一旦补上许可证，需要重新审视本 ADR 与 docs/UPSTREAM.md §3。

## ADR-004 环境 server 以子进程运行，建库调用 Python 接口

- 背景：AWM 的 `awm.core.server.run_server` 内部用 `os.system("python server.py | tee log")` 阻塞运行（`awm/core/server.py:163`），无法在进程内托管；建库则有可以直接调用的 Python 接口。
- 决定：
  - 建库调用 `awm.core.reset.reset_single_database`（`awm/core/reset.py:53-100`）；
  - server 用子进程 `python -m awm.core.server` 启动，并显式传 `--db_path`、`--temp_server_path`、`--output_dir`，避免 AWM 往数据集目录写临时文件（`awm/core/server.py:134-138`）；
  - 子进程放在独立的进程组里（`start_new_session=True`），停止时对整个进程组执行 `killpg`；
  - 不在进程内 import 生成的服务代码再用 uvicorn 启动：那要重写 AWM 对生成代码的补丁逻辑（`awm/core/server.py:92-143`），会偏离上游行为。
- 后果：
  - 每个环境多一次 Python 冷启动；
  - 只杀启动器进程时，`sh -c … | tee` 与真正的服务进程都会存活（docs/UPSTREAM.md §6.2），所以必须按进程组回收；
  - 容器里 PID 1 不回收僵尸进程，由 compose 的 `init: true` 处理；
  - 测试见 `tests/unit/test_env_manager.py` 与 `tests/integration/test_env_real_awm.py`。

## ADR-005 网关独立做成 MCP server

- 背景：智能体、外部 MCP 客户端和 UI 都要调用各场景的工具，治理规则必须统一：允许清单、风险分级、审批、限流、审计、错误规范化。
- 决定：
  - 核心逻辑在 `gateway/core.py`，是进程内可直接调用的 Python API；本仓库的 LangGraph 智能体直接调用它，省掉一次网络往返；
  - `gateway/server.py` 把同一个核心暴露为 MCP server（Streamable HTTP），对外是标准的 `list_tools` / `call_tool`，任何 MCP 客户端都能接入；
  - 工具名加 `<scenario>__` 前缀，避免不同场景冲突；会话由 `X-Workbench-Session` 请求头识别，审批令牌经 `X-Approval-Token` 请求头传递；
  - 不把治理写进智能体，否则外部客户端直连时不受约束；不做成普通 REST 服务，否则 MCP 客户端无法直接接入。
- 后果：要维护会话注册与令牌的传递方式；`list_tools` 按会话返回，客户端必须带会话请求头。

## ADR-006 默认拒绝（deny-first）与风险分级

- 背景：
  - AWM 场景里有大量写操作与破坏性操作，工具清单在运行时从上游拉取（ADR-009），同一场景重新合成后也可能出现新工具；
  - 只看工具名与描述首词的动词启发式会误判：名词 `list`、`view` 与读动词同形，例如 `DELETE purge_my_list_by_maturity_level`、`POST record_answer_view`；描述首词 "Get" 会把 `POST ensure_direct_dm_with_user` 判成读。对官方数据集静态扫描，18374 个 POST / PUT / PATCH / DELETE 工具中有 38 个按名称被判为 `read`，它们既不需要审批，成功时还可能被标为 `empty`（ADR-007）。
- 决定：
  - 允许清单：会话注册时一次性枚举允许的工具，之后上游新出现的工具仍被拒绝。不采用默认允许加黑名单。
  - 审批：`read` 直接放行；`write` 与 `destructive` 需要应用层签发的一次性 HMAC 令牌，令牌绑定会话、工具、参数摘要与预演结果（ADR-026），用一次即作废。每次调用最终由审批策略判定（ADR-027）；`configs/tool_policy.yaml` 的 `require_approval` 只能让 `read` 也需要审批，不能免除 `write` 与 `destructive` 的审批。
  - 启发式：工具名按 `_` 切词，加上描述的第一个词，按 `destructive` > `write` > `read` 的优先级匹配 `verbs` 表；识别不出动词时取 `unknown_default: write`。
  - HTTP 方法下限：`envs/catalog.route_methods` 用 `ast` 解析离线目录 `gen_envs.jsonl` 中 `full_code` 的 `@<obj>.<method>(...)` 装饰器，不执行代码。
    - 工具名取 `operation_id`；没有时按 FastAPI 0.115.12 的 `generate_unique_id` 推导（`fastapi/utils.py:179-184`，OpenAPI 在 `fastapi/openapi/utils.py:237` 使用它）。fastapi-mcp 0.4.0 以 operationId 作为工具名（`fastapi_mcp/openapi/convert.py:50-63`）。同一 operationId 出现在多个路由上时，取风险最高的方法。
    - DELETE → `destructive`；POST / PUT / PATCH → `write`；GET 与其它方法不设下限。`classify(..., http_method=...)` 先算启发式，再抬到下限；来源记为 `http_method`，理由中保留启发式原来的结论。
    - env-manager 启动会话时读取该场景的方法表，同名场景取最后一条记录，与 `awm/core/server.py:98-99` 一致；方法表经 `EnvInfo` 交给网关，远程 env-manager 的 HTTP 接口同样返回它，独立网关的 `POST /admin/sessions` 可选传 `tool_methods`。目录里查不到的工具沿用启发式与保守默认值。
    - 不扩充动词表或加停用词：名词与动词同形，治标不治本。不在运行时读取环境服务的 `/openapi.json`：那要依赖运行中的服务。
  - `overrides`：键为 `<tool>` 或 `<scenario>__<tool>`，是人工审核后的决定，按原样生效，即使低于下限，例如把以 POST 实现的纯查询设为 `read`。
  - `workbench gateway export-risk` 离线导出每个工具的分级，带 `http_method` 与 `heuristic_risk` 两列，供人工审核，也能暴露低于下限的覆盖。
  - 测试见 `tests/unit/test_gateway_policy.py`、`tests/unit/test_catalog.py`（与 FastAPI 实际生成的 OpenAPI 逐项比对）与 `tests/integration/test_official_data.py`。
- 后果：
  - 启发式仍会误判，例如 `submit_order…` 没有已知动词，落到默认的 `write`，实际更接近破坏性操作；靠 `overrides` 与 `export-risk` 审核修正；
  - 以 POST 实现的纯查询（例如 `get_trip_price_quote`）需要审批，人工确认变多，需要时用 `overrides` 调低；
  - 以 GET 实现的写操作识别不了，方法与语义不一致时下限帮不上忙；
  - 每次启动会话多读一次 `gen_envs.jsonl`，只对目标场景解码并解析代码；
  - 默认策略下每次写操作都需要人工确认（ADR-027）。

## ADR-007 区分"空结果"与"错误"

- 背景：
  - AWM 生成的环境代码不做错误处理，出错时一律是 HTTP 500，经 fastapi-mcp 转成 `isError` 文本；参数错误另有 `Input validation error: 'abc' is not of type 'integer'` 这类文本（docs/UPSTREAM.md §6.3）；
  - 没有结果时返回 `isError=False`，内容可能是 `[]`，也可能是包装对象：官方 `e_commerce_33` 上无结果的查询返回 `{"products": [], "total": 0}`，空购物车返回 `{"cart_id": 1, "items": []}`；
  - 写操作成功后也可能只返回空列表：官方 `social_media_4` 的 `patch_hidden_subreddits` 移除全部隐藏项后返回 `{"user_id": 1, "hide_subreddit_ids": [], ...}`；
  - 两者混淆时，模型会把"没找到"当成"失败"反复重试，或者反过来；把成功的写操作说成"没有匹配结果"，又会让模型以为写入没有生效。
- 决定：
  - `gateway/errors.py` 的 `normalize` 输出三态 `ok` / `empty` / `error`，从不静默返回空；
  - `error` 带结构化的 `code`、`hints`、`retryable`、`details`：枚举参数给出合法取值，缺失字段给出字段名，类型不符给出 `details.expected_type`；无法识别的上游错误文本退化为通用的 `upstream_error`；
  - `is_empty_payload` 判定空结果：空文本、`null`、`[]`、`{}`，或者对象中至少有一个列表字段、所有列表字段都为空、且没有嵌套对象；计数、ID、分页等标量字段不影响判定。不为每个工具配置"哪个字段是结果列表"：上千个场景无法人工维护；
  - `empty` 只用于风险级别为 `read` 的工具；`write` 与 `destructive` 调用成功时一律为 `ok`，由网关按分级传入 `normalize(..., read_only=...)`；
  - 网关的 MCP 前端用 `validate_input=False`，参数错误也经过同一套规范化；
  - 测试见 `tests/unit/test_gateway_misc.py` 与 `tests/integration/test_official_data.py`。
- 后果：
  - 这是启发式规则：`{"success": false}`，以及 `get_product_by_id` 对不存在的 ID 返回的占位对象（`id: 0`），都判为 `ok`；
  - 上游生成代码对不存在的外键也会直接写入，例如向购物车加入不存在的 offer，网关判为 `ok`，这类语义错误只能靠审批与 DB diff 暴露；
  - 规范化依赖上游错误文本的格式，格式变化时退化为 `upstream_error`，不会误判为成功；
  - 以 GET 实现、实际会写入的工具仍按 `read` 处理，可能被标为 `empty`，要用 `overrides` 纠正；以 POST / PUT / PATCH / DELETE 实现的由 HTTP 方法下限处理（ADR-006）。

## ADR-008 LLM 调用两层超时

- 背景：流式响应可能处于"半开"状态：连接一直不断，服务端隔一小段时间挤出一点数据。httpx 的 read timeout 计的是两次读之间的间隔，每收到一点数据就重新计时，这时永远不会触发。
- 决定：
  - 第一层是 httpx 分相位超时（connect / read / write / pool，`llm.connect_timeout_s`、`llm.read_timeout_s`），尽快发现连不上和完全不响应的服务端，这类错误可以重试；
  - 第二层是 `LLMClient.chat` 外层的 `asyncio.timeout(total_timeout_s)`，给包括重试在内的整次调用设墙钟上限；
  - 只对网络错误和 5xx 重试，采用带抖动的指数退避；4xx（包括 429）不重试；
  - 只设一层都不够：只有 read timeout 截不断半开连接，只有整体超时又要等满上限才发现连不上。
- 后果：墙钟上限要按最长的正常回答配置，设短了会误杀长回答，默认 `llm.total_timeout_s: 180`。`tests/unit/test_llm_client.py` 用假服务器覆盖两种情况：慢响应触发读超时并重试，半开连接由墙钟上限截断。

## ADR-009 工具清单在运行时从 `list_tools` 拉取

- 背景：AWM 各场景的工具由生成代码决定，场景之间差别很大，同一场景重新合成后也可能变化。
- 决定：
  - intake 节点在每个会话开始时调用 `gateway.list_tools(session)`，拿到的清单已按允许清单过滤并带风险级别，再渲染进 prompt 与 LLM 的 `tools` 参数（act 调用的渲染方式见 ADR-016）；
  - prompt 文件只写行为规则，不出现任何具体工具名，版本记录见 docs/ARCHITECTURE.md §4；
  - 不在 prompt 里手写工具说明：无法覆盖上千个场景，也跟不上重新合成。
- 后果：每个会话开始多一次 MCP 往返；prompt 与请求的长度随工具数量增长。测试见 `tests/unit/test_agent_parts.py`。

## ADR-010 长期记忆按来源设写入门槛

- 背景：模型的推断一旦写进长期记忆，会在以后的会话里被当成事实反复使用，放大错误。
- 决定：
  - 每条记忆带来源标记 `user_stated` / `tool_result` / `inferred`；`inferred` 一律拒绝写入，拒绝事件记入 trace；
  - 条目设 TTL，提供 `GET /memory` 与 `DELETE /memory/{key}`；
  - 存储用 LangGraph 的 `SqliteStore`；
  - 全存会放大错误，全不存又失去跨会话的用户陈述与工具结果，所以按来源设门槛；
  - 测试见 `tests/unit/test_agent_parts.py` 与 `tests/unit/test_agent_e2e.py`。
- 后果：
  - 来源标记由模型自己填写，门槛只能拦住它自己承认是推断的内容，挡不住谎报来源；
  - `SqliteStore` 的过期判断只精确到秒（docs/UPSTREAM.md §10.1）。

## ADR-011 自合成环境与官方数据隔离

- 背景：AgentWorldModel-1K 是被评测的数据，本仓库不改动它。AWM 的 `gen scenario` 会把结果写回输入的种子文件（`awm/core/scenario.py:642`），`env start` 默认往数据集目录写临时文件（`awm/core/server.py:134-138`）。
- 决定：
  - 每次合成使用独立目录 `data/synth/<run_id>/`，输出目录不在 `data/synth/` 之下时直接报错；不在 `data/awm1k/` 或 AWM 的 `outputs/` 里合成；
  - 种子文件先复制进运行目录再使用；
  - `manifest.json` 标记 `origin: local-synth`、`official_data: false`；
  - 默认只做 dry-run，显式加 `--execute` 且 LLM 相关环境变量齐全时才真正执行；
  - LLM 请求经本地代理转发，由代理实现缓存、重试与按步骤记账；真实 API key 只由代理持有，AWM 子进程拿到的是占位 key（ADR-017）；
  - 测试见 `tests/unit/test_synth.py`。
- 后果：多了一层本地代理，AWM 自身的重试与代理的重试会叠加。代理的账本同时是预算熔断与失败判定的依据（ADR-021、ADR-022）。

## ADR-012 不创建 `paper_mirror`；smoke 用 AgentFly 内置工具与奖励

- 背景：AWM 相关的公开内容只有环境适配，没有完整的官方训练配方（docs/UPSTREAM.md §7.3），因此没有可以镜像的配方。AgentFly 的工具与奖励在 import 时注册，训练入口不会自动加载外部插件。
- 决定：
  - 不创建 `paper_mirror.yaml`：按论文文字自行拼出的配方并不是论文的配方，也容易被误当成复现；
  - 不写 AWM 工具与奖励插件：它在每个 Ray worker 中的加载方式要在完整训练环境里才能确认，做不到就只是一个看似可用的空壳；
  - smoke 只演示"rollout → 奖励 → 更新"：使用 AgentFly 自带的 `calculator` 工具与 `math_equal_reward_tool`，模型不超过 1.7B、LoRA、不超过 5 步，产物目录带 `NO_RESULTS` 标记；
  - smoke 配置的 Hydra 键跟随固定 fork 的实际配置结构，由 `workbench train preflight` 校验（docs/UPSTREAM.md §7.2）。
- 后果：smoke 不接触 AWM 环境，只说明训练链路能打通，与 AWM 本身无关；自训的权重不代表 Arctic-AWM。把 AWM 环境接入 AgentFly 的 RL 训练不在本仓库范围内；批量实验在 AWM 任务上用 LoRA SFT 训练小模型，不经过 AgentFly（ADR-028）。

## ADR-013 不做效果评测

> 适用范围：工作台的应用层功能（网关、审批、守卫、API）。官方任务上的批量实验另有设计，见 ADR-028。

- 背景：本仓库处理模型的使用、部署、管理与观察。被评测的对象，即 Arctic-AWM 模型、AgentWorldModel-1K 数据与官方评测 harness，都来自上游。
- 决定：
  - 应用层不做模型评测：不跑 `awm bench` 以及 BFCLv3、τ²-bench、MCP-Universe 的 harness，也不批量跑任务后汇总任何比率；
  - 论文数字只登记在 `results/registry.yaml`，每条带 `source` 与 `verified`，条目对照 arXiv 2602.10090 v3 的 Table 4 核对；引用处附注"论文报告值，由官方模型在官方评测 harness 上测得，不是本仓库应用层的测量结果。"，docs/RESULTS.md 由 registry 生成；
  - `make check-numbers` 扫描 README 与 docs，拦截未登记或缺少免责声明的百分比、两位小数的分数与 Pass@k 数字，以及描述应用层效果的措辞；
  - `awm agent` / `awm verify` 只作为单任务的链路检查运行，结果不写成比率或效果结论；
  - 应用层（网关、审批、守卫）只描述机制，用单元测试与集成测试说明机制按设计工作；
  - 不在官方 harness 上复现论文数字：那需要 GPU 与评测 harness，也不是应用层的职责。不自建小任务集统计应用层带来的变化：样本量与实验设计都不足以支撑结论，容易误导。
- 后果：应用层的测试不回答"用了网关或审批之后任务完成得更好吗"。回答这类问题需要单独的实验设计：固定模型、固定任务集、足够的样本与预先登记的指标，ADR-028 的批量实验按这些要求设计。

## ADR-014 审计脱敏不把时间戳当电话

- 背景：网关审计摘要对工具结果中的邮箱、电话、卡号做脱敏。电话正则如果只要求匹配不紧跟在单词字符之后，会把时间戳当成电话：AWM 写入的 `created_at` 带微秒，`17:05:31.506430` 中的 `31.506430` 前面是冒号，被当成 8 位号码，变成 `17:05:[PHONE]`；官方数据库行中空格分隔的 `YYYY-MM-DD HH:MM:SS` 会连日期带小时变成 `[PHONE]:MM:SS`。
- 决定：收紧 `gateway/audit.py` 的 `PHONE` 正则本身，把规则集中在一个正则里，不另写时间戳正则再跳过重叠的匹配。在原正则上加三条限制：
  - 匹配不得从单词字符或小数点之后开始（`(?<![\w.])`）；
  - 不得紧跟在"数字:"之后开始，排除秒与微秒；
  - 不得紧接在":数字"之前结束，排除"日期 小时"；
  - 纯日期 `YYYY-MM-DD` 仍由 `_mask_phone` 保留；
  - 测试见 `tests/unit/test_gateway_misc.py`：多种时间戳写法原样保留，各种电话写法仍被脱敏，包括 `tel:+1…`、国家码、括号、点号分隔、与时间戳同在一行。
- 后果：紧跟在"数字:"之后、或紧接着":数字"的电话号码不再被脱敏，例如 `ext1:5551234567`；非 ISO 格式的日期（例如 `31.12.2024`）仍可能被当成电话。脱敏是启发式的，只作用于审计摘要，trace 不做脱敏。

## ADR-015 回传 reasoning_content（DeepSeek 思考模式）

- 背景：
  - DeepSeek 默认开启思考模式。思考模式指南（https://api-docs.deepseek.com/guides/thinking_mode）规定：思维链经与 `content` 同级的 `reasoning_content` 返回，流式响应中为 `delta.reasoning_content`；请求带 `tools` 时，之前所有轮次的 `reasoning_content` 都要完整回传，没有工具调用的轮次也一样，否则 API 返回 400；不带 `tools` 时不需要回传，传了也会被忽略。
  - act 节点自己构造历史消息，只保存 `content` 与 `tool_calls`；LLM 层拿不到智能体状态。
- 决定：
  - 改动限定在 `src/workbench/llm/`，不加 `extra_body` 开关，智能体代码不变；
  - 后端从流式的 `delta.reasoning_content` 或非流式的 `message.reasoning_content` 读取思维链，放进 `ChatResult.reasoning_content`，智能体不使用它；
  - `llm/reasoning.py` 的 `ReasoningStore`：每得到一个带 `reasoning_content` 的回复，就以"之前的非 system 消息 + 该回复的 content 与第一个工具调用 id"为键记下来；之后的请求只要带 `tools`，就给其中能认出的每条 assistant 消息补上 `reasoning_content`，补在消息的副本上，不修改调用方的字典；不带 `tools` 的请求不补；调用方自带 `reasoning_content` 的消息保持原样；
  - 识别时不看 system 消息：重新规划会重建 system prompt，其余历史不变；
  - 是否回传只取决于服务端是否返回过 `reasoning_content`，不按 URL 判断。不返回它的服务端，例如一般的 OpenAI 兼容服务、未启用 reasoning parser 的 vLLM，收到的请求体不变；
  - 文档没写清的情况不猜：没有 `reasoning_content` 的历史轮次，例如非思考模式产生的、来自其它后端的、进程重启后丢失的，原样发送，不补空值。API 参考只把请求消息中的 `reasoning_content` 描述为 Chat Prefix Completion（Beta）的输入，与思考模式指南不一致，本仓库按思考模式指南实现；
  - 测试见 `tests/unit/test_llm_reasoning.py`：假服务端按文档行为，对缺少 `reasoning_content` 的带 `tools` 请求返回 400，真实的智能体图对它完整跑通。
- 后果：
  - 记录只在进程内存中：服务重启后从 checkpoint 恢复的会话丢失了历史轮次的 `reasoning_content`，按文档可能得到 400，智能体以 `llm_error` 终止；
  - 每个后端实例最多记 1024 条，按 LRU 淘汰，极长或大量并发的对话可能丢掉较早的轮次；
  - 两个会话的非 system 历史完全相同时共用一条记录；
  - 多个工具调用的轮次只按第一个调用 id 识别，因为 act 节点只保留第一个调用。

## ADR-016 act 只经原生 `tools` 参数传工具定义；预算默认值

- 背景：
  - act 请求带原生 `tools` 参数，服务端据此生成结构化的工具调用。如果 system prompt 里再放一份完整的描述与参数 schema（`tools_block`），定义就发了两遍。
  - 在官方 `e_commerce_33`（39 个工具）上离线计算：两份定义时一次 act 调用 25640 token，其中原生 `tools` 13231 token；只经原生 `tools` 传定义时为 14358 token。计算使用 `deepseek-ai/DeepSeek-V4.1-Flash` @ `dba1be0` 的 `tokenizer.json` 与官方编码脚本 `encoding/encoding.py`（MIT），结果与 DeepSeek 计费的 `prompt_tokens` 一致。
- 决定：
  - `agent/nodes/common.py` 的 `act_system_prompt` 只组合 act 提示词、计划和 `tools_risk_table`，后者每个工具一行：名称、风险级别、是否需要审批；描述与 schema 只出现在原生 `tools` 参数中。act 提示词 v2 写明"描述与参数 schema 随请求的 tools 提供，下表只列名称与风险级别"（docs/ARCHITECTURE.md §4）；
  - plan 请求不带 `tools` 参数，仍用 `tools_block` 提供完整定义；
  - 不反过来只保留 system prompt 中的定义：那会失去原生工具调用，服务端也不再能按 schema 生成调用；
  - `agent.token_budget` 默认 240000，使 39 个工具的官方场景跑满 `max_steps`=12 次 act 仍在预算内，先触发步数上限。按官方 `e_commerce_33` 上一次 DeepSeek 运行的计费推算：`12627 + Σ(14482 + 659k, k=0..11) + 1804 = 231709`，向上取整。三项分别是 plan、act、verify；14482 是第一次 act 调用，659 是相邻 act 调用的平均增长；
  - `llm.max_tokens` 默认 8192：同一次运行中每次调用的输出（含思考 token）最多 960，取 8 倍再向上取到 2 的幂；
  - 测试见 `tests/unit/test_agent_parts.py`。
- 后果：
  - 模型只能从原生 `tools` 读到工具描述，服务端必须把 `tools` 渲染进提示词。DeepSeek 这样做，上面的离线编码可以看出。vLLM v0.19.0 对带 `tools` 而没有 `tool_choice` 的请求默认 `"auto"`（`vllm/entrypoints/openai/chat_completion/protocol.py:640-643`），未配置 tool parser 时直接返回错误 `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set`（`vllm/entrypoints/serve/render/serving.py:197-215`），配置之后才把 `tools` 交给 chat template 渲染（同文件 `:223-247`），因此 Arctic-AWM 的 serving profile 启用了 `hermes`（ADR-018）；
  - 预算默认值来自一个参考场景的一次运行；工具更多或工具结果更长时会先触发预算，它仍是成本保护；
  - 思考长度随任务难度变化；DeepSeek 思考模式的默认输出上限是 64K，只按实际输出计费，`max_tokens` 设得过小会截断回答；
  - plan 请求仍带完整定义，约 12K token。

## ADR-017 执行生成代码的子进程只拿白名单环境变量

- 背景：
  - AWM env server 执行场景中由 LLM 生成的 `full_code`（`awm/core/server.py:163`）；`awm env reset_db` 执行生成的 SQL；`awm env check_all` 逐个启动生成的 server，`awm/core/env.py:161-172` 的 `Popen` 没有传 `env=`，这些 server 继承它的环境；`gen env` 与 `gen verifier` 调用 LLM，也执行自己生成的代码（`awm/core/env.py:161-172`、`awm/core/verifier.py:104`）。
  - 把 `dict(os.environ)` 交给这些子进程，生成代码就能读到 `DEEPSEEK_API_KEY`，以及容器里的云令牌和 git 令牌。
- 决定：deny-first 的环境变量白名单。不用黑名单：按名字删除 `*_API_KEY`、`*_TOKEN` 会漏掉名字不规则的秘密，例如 `GIT_CONFIG_VALUE_0` 中的认证头、代理 URL 中的凭据。
  - 不调用 LLM 的子进程用 `generated_code_env()`，只保留 `BASE_VARS`：`PATH`、`LANG`、`LANGUAGE`、`LC_ALL`、`LC_CTYPE`、`TZ`、`TMPDIR`、`PYTHONIOENCODING`、`PYTHONUTF8`、`PYTHONUNBUFFERED`、`PYTHONPYCACHEPREFIX`，最后一项缺省时补为 `.cache/pycache`。使用者是 env server（`envs/manager.py`），以及 `SynthRunner.validate` 与 `workbench synth validate` 调用的 `reset_db`、`check_all`，它们拿不到任何 key。
  - gen 步骤在白名单之上只加 LLM 设置（`SynthRunner.step_env`）：
    - 经本地代理时，只加占位 key `workbench-proxy`、代理地址和 `AWM_SYN_OVERRIDE_MODEL`，上游 key 留在 workbench 进程的代理线程里；步骤只连本机代理，不传出站代理变量。`workbench synth run --execute` 总是经代理；
    - 不经代理时，只能在 Python 中直接调用 `execute(proxy_base=None)`，此时加 `LLM_VARS` 与 `NETWORK_VARS`：前者是 AWM 读取的 LLM 变量（`awm/gpt.py:38-55`、`awm/tools.py:407-432`、`awm/core/scenario.py:63-84`），后者是出站代理与 CA 设置。
  - 这些变量足够的依据：`PATH` 必需，AWM 用 `sh` 管道把 server 输出交给 `tee`（`awm/core/server.py:163`）；解释器是 venv 中的 `sys.executable`，不依赖 `VIRTUAL_ENV` 或 `PYTHONPATH`；`PORT` 与 `DATABASE_PATH` 由 AWM 启动器自己设置（`awm/core/server.py:157-158`）；不传 `HOME`，子进程需要时仍能从 passwd 查到家目录（docs/UPSTREAM.md §6.8）。
  - 附带效果：父进程设置的 `HOST` 不再覆盖 `--host`，生成代码读的是 `os.environ.get('HOST', ...)`（`awm/core/server.py:114`）。
  - 只执行固定命令的子进程不在范围内，例如 `git rev-parse`、`nvidia-smi`，仍继承完整环境；训练进程另见 ADR-024。
  - 测试见 `tests/unit/test_env_manager.py`、`tests/integration/test_env_real_awm.py`（逐个读取 AWM 启动器、`sh`、`tee` 与生成 server 的 `/proc/<pid>/environ`）、`tests/integration/test_synth_validate_real_awm.py`，以及白名单模块自己的单元测试。
- 后果：
  - 白名单固定在代码中，没有配置开关；部署确实需要额外变量时只能改代码；
  - 只隔离环境变量：子进程与 workbench 以同一用户运行，仍能读取该用户可读的文件，例如 `.env`、`~/.aws/credentials`；隔离文件需要另一个用户或容器沙箱，不在本仓库范围内；
  - `gen env` 与 `gen verifier` 的生成代码经代理时仍能看到占位 key 和代理地址，可以经代理发起 LLM 调用，产生费用并记入账本，但拿不到上游 key；不经代理时会看到真实的 `OPENAI_API_KEY`。不改上游就无法把这两个步骤的"调用 LLM"与"执行生成代码"分开；
  - `awm verify` 在同一进程里执行 verifier 代码并读取裁判的 key，由 `workbench verify` 隔离（ADR-023）；
  - `awm agent --scenario` 自动起服时，server 继承 `awm agent` 的环境（`awm/core/server.py:174-195` 的 `Popen` 没有传 `env=`），因此本仓库只用 `--mcp_url` 模式连接 env-manager 启动的 server。

## ADR-018 vLLM serving profile 启用 `hermes` tool parser

- 背景：智能体的 act 请求带原生 `tools`，vLLM v0.19.0 在没有开启 `--enable-auto-tool-choice` 与 `--tool-call-parser` 时直接拒绝这类请求（ADR-016）。Arctic-AWM 的模型卡没有指定 parser。
- 决定：`configs/serving/arctic-awm-4b.yaml` 设 `enable_auto_tool_choice: true`、`tool_call_parser: hermes`，`reasoning_parser` 与 `chat_template` 不设置。`workbench serve vllm-cmd` 与 `scripts/serve_vllm.sh` 从 profile 生成参数；`docker-compose.yml` 的 `vllm` 服务命令与 `workbench serve vllm-cmd` 的输出一致，只把 `--host` 设为 `0.0.0.0` 供其它容器访问，并带 `--served-model-name`，由 `tests/unit/test_llm_client.py::test_compose_vllm_matches_the_serving_profile` 断言两者一致。选择 `hermes` 的源码依据（docs/UPSTREAM.md §9）：
  - 模板要求的格式：`Snowflake/Arctic-AWM-4B` @ `437dfa0` 的 `chat_template.jinja` 在带 `tools` 时要求模型输出 `<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>`（第 11 行），历史中的工具调用按同一格式渲染（第 65-73 行）；`tokenizer_config.json` 中 `<tool_call>` 与 `</tool_call>` 是 id 151657 与 151658 的 added token，`special: false`，默认解码（`skip_special_tokens=True`）不会删掉它们；
  - `hermes` parser 解析同一格式（`vllm/tool_parsers/hermes_tool_parser.py`）：非流式用正则 `<tool_call>(.*?)</tool_call>|<tool_call>(.*)` 取出标签之间的文本（`:61-66`），按 JSON 读取 `name` 与 `arguments`（`:106-126`），第一个 `<tool_call>` 之前的文本作为 `content`（`:128`）；流式同样按标签切分（`:160-208`）。它在 `vllm/tool_parsers/__init__.py:61-64` 注册，只做文本匹配，不要求标签在词表中；
  - 不带 `tools` 的请求不受影响：`tool_choice` 默认 `"none"`（`chat_completion/protocol.py:175-181`），只有带 `tools` 时才改成 `"auto"`（`:642-643`）；`"none"` 时预处理不调用 parser 的 `adjust_request`（`serve/render/serving.py:534-550`）；非流式只在 `"auto"` 或 `None` 时解析（`engine/serving.py:920-924`）；流式要求 `request.tools` 非空才创建 parser（`chat_completion/serving.py:532-535`、`:559-571`、`:1752-1757`）；
  - `awm agent` 正是这种请求：它只发 `model`、`messages`、`max_completion_tokens`、`temperature` 和 vLLM 的 `extra_body`，不带 `tools` 与 `tool_choice`（`awm/core/agent.py:367-381`），它的 `<tool_call>` 文本协议照旧留在 `content` 里，由 AWM 自己解析（`awm/core/agent.py:130-167`）；
  - workbench 的 `openai_compat` 后端（`vllm` 后端复用它）收到原生 `tool_calls` 时直接使用，并从 `content` 中去掉 `<think>`；没有原生调用时再从正文解析 `<tool_call>`（`src/workbench/llm/backends/openai_compat.py:187-198`），所以启用 parser 不需要改客户端代码。
- 后果：
  - 不设 reasoning parser，思考内容 `<think>…</think>` 留在 `content` 中，由 workbench 客户端去掉；模型如果在思考内容里写出 `<tool_call>` 标签，parser 会把它当成工具调用。`qwen3` reasoning parser 可以把思考内容分出去，模型卡没有要求，这里不启用；
  - compose 的命令是写死的，改 profile 时要同步改 compose，由上面的单测防止两者漂移。

## ADR-019 合成可以从 `gen task` 开始（`--scenario-file`）

- 背景：
  - `gen scenario` 必须有 embedding 端点：`awm/core/scenario.py:63` 断言 `EMBEDDING_OPENAI_API_KEY` 存在，随后用它建 embedding 客户端（`:83-86`）。只提供对话模型的 LLM 服务无法运行这一步，例如 DeepSeek API 没有 embeddings 接口。
  - 上游 CLI 支持从场景文件开始：`awm gen task` 的 `input` 字段注释就是 "scenario description file"（`awm/core/task.py:13`），CLI 中为必填；`run` 直接加载这个文件（`:126-129`），每条只读取 `name` 与 `description`（`:44-45`）；官方 `gen_scenario.jsonl` 的 1000 条都只有这两个字符串字段。
- 决定：`workbench synth run --scenario-file` 从步骤计划里去掉 `gen scenario`，把文件复制为运行目录中的 `gen_scenario.jsonl`，作为 `gen task` 的输入（`src/workbench/synth/runner.py`、`cli.py`）。不手工写 `state.json` 把 `scenario` 步骤标成已完成：那样绕过编排层，manifest 也不会记录场景来源。
  - 每行必须恰好是 `{"name": str, "description": str}`。名称必须是 AWM 规范化后的形式（`awm/tools.py:335-339`），以 `local_` 开头且不以 `_<数字>` 结尾。只靠前缀不够：官方 revision `dde80a0` 的 1000 个场景名全部是 `<类别>_<序号>`，其中 `local_search_1` 与 `local_services_marketplace_1` 也以 `local_` 开头；
  - 官方数据在本地时，名称不得与 `env.dataset_dir` 下 `gen_scenario.jsonl` 中的任何一个相同，manifest 的 `official_name_check` 记录这项检查是否执行（ADR-011）；
  - `--scenarios` 不能超过文件中的条数；运行目录中已有内容不同的 `gen_scenario.jsonl` 时报错，内容相同则继续，以便续跑；
  - 这种模式下 `--execute` 只要求 `OPENAI_API_KEY` 与 `AWM_SYN_OVERRIDE_MODEL`；manifest 记录 `skipped_steps: ["scenario"]`，以及场景文件的路径与 sha256；
  - 不传该参数时仍从 `gen scenario` 开始，仍要求 embedding key；
  - 测试见 `tests/unit/test_synth.py`。
- 后果：场景文件代替了 `gen scenario` 的生成与去重；`local_` 前缀与"不以 `_<数字>` 结尾"是本仓库的约定，不是 AWM 的要求，后者依据的是官方数据的命名方式。

## ADR-020 合成中断与续跑：步骤独立成进程组，没有完成的步骤从头重做

- 背景：
  - 如果 runner 在自己的进程组里启动步骤：SIGTERM 只发给 runner 时，Python 的默认处理直接结束 runner，步骤继续运行；终端 Ctrl-C 发给整个前台进程组，runner 与 AWM 各自处理，结果取决于时机。
  - AWM 用 `start_new_session=True` 启动它要测试的 server（`awm/core/env.py:161-172`），这些 server 不在步骤的进程组里；只有正常路径上 AWM 自己用 `killpg` 回收它们（`:212-227`），步骤被杀后它们成为孤儿。
  - 续跑按步骤进行，已付费的请求由代理缓存原样重放（ADR-011）。AWM 各步骤写输出的方式不同（docs/UPSTREAM.md §6.7）：
    - 6 个步骤都在结束时一次覆盖写出，见 `awm/core/scenario.py:629`、`task.py:142`、`db.py:259`、`sample.py:282`、`spec.py:120`、`env.py:568`；
    - `gen verifier` 每处理一批就追加写入（`awm/core/verifier.py:172-176`），它自带的续跑只重新生成校验不通过的行，新行追加在旧行后面（`:145-156`、`:260-308`、`:456`）；
    - `awm verify` 取第一条匹配的行（`awm/tools.py:456-472`），所以 `gen verifier` 中途中断后直接重跑，排在前面的旧的无效行会被选中。
- 决定：由 runner 负责回收与重做（`src/workbench/synth/runner.py`、`src/workbench/envs/procs.py`、`src/workbench/cli.py`）。
  - 启动：每个步骤以 `start_new_session=True` 启动，stdin 接 `/dev/null`，终端的 Ctrl-C 只到达 runner；`execute()` 运行期间把 SIGTERM 转成 `KeyboardInterrupt`，与 SIGINT 走同一条路径。
  - 回收：趁步骤进程还在，按 `/proc/<pid>/stat` 中的 ppid 建树，找出全部后代；对它们所在的每个进程组先发 SIGTERM，最多等 5 秒，仍存活的再发 SIGKILL；永远不向 runner 自己所在的进程组发信号。
  - 状态：被中断的步骤记为 `interrupted`，CLI 以退出码 130 结束，用同一条命令续跑；最后的验证（`reset_db`、`check_all`）经同一个 runner 执行，中断时同样回收。
  - 重做：没有完成的步骤重做前，已有输出移到 `attempts/<步骤>.<n>/`，不删除，`state.json` 中该步记录 `set_aside`；每次尝试都从没有输出的状态开始，结果与一次不中断的运行相同。
  - 测试见 `tests/unit/test_synth_resilience.py`：真实子进程加监听真实端口的假上游（`tests/unit/synth_harness.py`），分别在一次写出的步骤（`db`）和追加写出的步骤（`verifier`）中间向 runner 发 SIGTERM，检查测试 server 被回收、续跑以 0 退出、每个请求只到达上游一次、输出与不中断的运行相同。
- 后果：
  - 靠 `/proc` 找后代，只适用于 Linux；步骤退出后它的子进程会被挂到别的父进程下，所以必须在步骤还在运行时回收，中断发生时正满足这一点；
  - runner 自己被 SIGKILL 时无法回收；
  - AWM 被 SIGTERM 结束时不执行 `finally`，`gen env` 与 `check_all` 的临时目录 `/tmp/env_test_*`（`awm/core/env.py:148`、`:230-235`）会残留，需要手动删除；
  - 移开输出后，AWM 在 `gen env`、`gen verifier` 中自带的续跑不再起作用；请求正文相同的由缓存重放，正文不同的会再次计费，受预算熔断约束（ADR-021）。

## ADR-021 合成预算熔断：本地代理按账本累计费用拒绝转发

- 背景：
  - 账本汇总 `ledger_summary.json` 只在运行结束时生成，执行过程中没有费用上限。
  - 代理拒绝请求时 AWM 进程不会失败：openai SDK 2.38.0 把 402 映射为普通的 `APIStatusError`（`openai/_client.py:1081-1112`），自己也不重试 402（`openai/_base_client.py:795-826`）；AWM 的 `GPTClient` 把它当作一般异常，共尝试 3 次，间隔 3 秒、6 秒（`awm/gpt.py:168-204`），仍失败就返回内容为空的 refusal completion（`:205-206`、`:103-131`），步骤继续执行，可能以 0 退出。所以 runner 不能只凭退出码判断预算熔断。
- 决定：代理在每次转发前检查，runner 在步骤开始前与结束后各检查一次（`src/workbench/synth/` 下的 `proxy.py`、`ledger.py`、`runner.py`，以及 `src/workbench/config.py`、`src/workbench/cli.py`、`configs/app.yaml`）。只在步骤之间比较累计费用不够：单个步骤内部就可能远超上限。
  - 配置：`synth.budget`，默认 `5.0`，单位是价格表 `configs/pricing.yaml` 的币种 CNY；可用 `WORKBENCH_SYNTH__BUDGET` 覆盖，设为 `null` 时关闭熔断。
  - 计费口径与账本汇总相同：只计未命中缓存、也未被拒绝的请求，按价格表计算上界（`Ledger.spent`）。
  - 代理：缓存命中照常返回，不受上限影响；需要转发的请求在以下情况拒绝转发，返回 HTTP 402（`type: budget_exceeded`），并在账本中记一条 `refused: true`：已花费不低于上限；请求的模型不在价格表中，或账本中已有没有价格的模型。无法计价时不放行（fail closed）。
  - runner：步骤开始前检查同样的条件，成立就不启动该步骤（`SynthBudgetExceeded`）；步骤结束后，只要本步有 `refused` 条目，就记为 `failed`、`reason: "budget"`，不看退出码；CLI 以退出码 1 结束，提示提高 `synth.budget` 后用同一命令续跑，续跑时先移开该步的输出（ADR-020）。
  - 不加 `--execute` 时，dry-run 输出中的 `budget` 列出上限、币种和已花费。
  - 只在经代理执行时生效，`workbench synth run --execute` 总是经过代理。
  - 测试见 `tests/unit/test_synth_resilience.py`：假上游每次调用计 ¥1，假步骤像 AWM 一样把错误变成空回复并以 0 退出，runner 仍判该步失败；提高上限之后，续跑成功，每个请求只付费一次。
- 后果：
  - 在途超支：检查发生在转发之前，已转发、还在途的请求照常完成并计费。超出部分最多是超限那一刻所有在途请求的费用之和；在途请求数受 AWM 各步骤的并发设置约束，`GPTClient` 默认 64（`awm/gpt.py:36`）；按价格表上界和思考模式默认 64K 的输出上限计算，单个请求的输出部分最多约 ¥0.5。
  - AWM 会再试 2 次，每个被拒绝的请求在账本中有 3 条 `refused` 记录，步骤也多等约 9 秒。
  - 每次转发前都重新读取整本账本，请求很多时有额外开销，本仓库的运行规模下可以忽略。
  - 价格表只有 `deepseek-flash`；换用其它模型前，包括 `gen scenario` 需要的 embedding 模型，要先在价格表中加上价格，或显式设 `budget: null`。

## ADR-022 上游错误记入账本，按阈值判定合成步骤（`done_with_failures`）

- 背景：
  - AWM 的 `GPTClient` 把失败的请求变成空回复，不抛异常（`awm/gpt.py:168-206`），步骤写出空结果后以 0 退出。代理如果只记录成功的响应、runner 只看退出码与输出文件，这样的步骤会被记为完成。
  - 同一个请求可能被三层重试，后两层在代理看来都是正文相同的新请求：代理自己对 5xx 与网络错误重试（`max_retries`）；openai SDK 2.38.0 对 408、409、429、5xx 重试 2 次（`openai/_base_client.py:795-826`）；AWM 对任何错误共尝试 3 次（`awm/gpt.py:168-204`）。
- 决定：按请求正文（缓存键）归并，同一请求的所有尝试算一个请求，由最后一次尝试决定结局，只有最终没有得到回答的请求才算丢失。不按失败次数计数：AWM 重试 3 次的一个请求会被算成 3 次，之后成功了的请求也会被算作失败。实现在 `src/workbench/synth/` 下的 `proxy.py`、`ledger.py`、`runner.py`、`validate.py`，以及 `src/workbench/config.py`、`src/workbench/cli.py`、`configs/app.yaml`。
  - 代理：4xx（含 402、429）不重试，原样返回给步骤，同时记一条 `failed: true` 与上游状态码；5xx 与网络错误在代理重试完之后返回 502，同样记 `failed: true`，状态码取最后一次，网络错误记作 `network`；每条账本记录带请求的缓存键 `key`；错误响应不是 JSON 时（例如 HTML 错误页），包装成 JSON 错误返回。
  - 统计（`step_requests`）：按 `key` 归并一个步骤的账本记录，得到被拒绝的请求数与最终失败的请求数，后者按最后一次的状态码或 `network` 分类；没有 `key` 的旧记录各算一个请求。
  - 判定（`judge_step`，与预算熔断统一），按顺序：
    1. 有被拒绝的请求 → `failed`，`reason: budget`（ADR-021），不受阈值影响；
    2. 最终失败的请求数超过 `synth.max_failed_requests` → `failed`，`reason: upstream_errors`，CLI 以退出码 1 结束，提示修复上游或提高阈值后用同一命令续跑；
    3. 退出码非 0 或输出缺失 → `failed`；
    4. 最终失败的请求数在 1 到阈值之间 → `done_with_failures`；
    5. 其余 → `done`。
    - `state.json` 中该步记录 `failed_requests` 与 `failed_by_status`，有被拒绝的请求时记录 `refused_requests`。
  - 续跑：`done_with_failures` 只在失败数不超过当前阈值时算作完成，调低阈值后用同一命令续跑会重做这一步；某一步被重做后，其后的所有步骤也重做，因为它们的输入可能变了，正文不变的请求由缓存重放。
  - 输出：CLI 的步骤状态带失败数，`done_with_failures` 另有一行黄色提示；`ledger_summary.json` 每步有 `failed_calls`，即失败的尝试次数；`validation.json` 有 `gen_steps_with_failed_requests`，`validation.md` 列出这些步骤，`workbench synth validate` 同样适用。
  - 配置：`synth.max_failed_requests` 默认 0，可用 `WORKBENCH_SYNTH__MAX_FAILED_REQUESTS` 覆盖，不能为负数。
  - 测试见 `tests/unit/test_synth_failures.py`：步骤是真实子进程，代理是真实的，错误由脚本化的 transport 注入。
- 后果：
  - 只有经代理执行时才能这样判定；AWM 内部的其它失败，例如解析模型输出失败、生成的代码校验不通过，不是上游错误，不在账本里，仍只能看 AWM 的退出码与输出；
  - 5xx、网络错误与超时的请求可能已在上游计费，账本只能按 0 计；
  - 同一步骤里正文完全相同的两个请求，会被当作同一个请求的两次尝试；
  - `done_with_failures` 的步骤输出缺少那些请求的结果，下游步骤照常运行；阈值大于 0 就意味着接受不完整的产物。

## ADR-023 `workbench verify`：`awm verify` 不拿到任何 key

- 背景：
  - `awm verify` 在同一个进程里做两件事，不改上游就无法分开：执行数据集中的 verifier 代码，namespace 里直接提供了 `os`（`awm/core/verify.py:104-126`、`:151-174`）；sql 模式下从同一进程的环境变量读取裁判的地址与 key（`:416-433` 调用 `resolve_llm_config`，`awm/tools.py:386-437`）。在带 key 的 shell 里直接运行 `awm verify`，verifier 代码能读到这些 key。
  - AWM @ `85e322f` 中只有 `--mode sql` 调用 LLM 裁判；`--mode code` 执行确定性的 verifier，直接返回 `complete` 或 `others`（`awm/core/verify.py:416-433`），配置检查也只针对 sql 模式（`:51-80`）。官方数据集同时提供 `gen_verifier.jsonl`（sql）与 `gen_verifier.pure_code.jsonl`（code），默认路径见 `:376-379`（docs/UPSTREAM.md §6.5）。
- 决定：新增包装命令 `workbench verify`（`src/workbench/verify.py`、`src/workbench/cli.py`），做法与合成流水线相同（ADR-011、ADR-017）。只在文档里要求"先清空环境再运行"依赖使用者自觉，也无法测试。
  - 参数：`--input` 是 `awm agent` 的输出目录；`--mode` 为 `code` 或 `sql`，默认 `code`；`--verifier` 默认取 `env.dataset_dir` 下对应模式的官方文件；`--init-db`、`--final-db` 是初始与最终数据库；`--judge-model` 是 sql 模式的裁判模型，默认读 `AWM_SYN_OVERRIDE_MODEL`。
  - code 模式：子进程只拿到 `generated_code_env()` 的白名单（ADR-017），没有任何 key。
  - sql 模式：裁判的上游地址与 key 从 workbench 进程的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 读取，与 `synth.upstream_*_env` 相同；`awm verify` 子进程在白名单之外只拿到 `AWM_SYN_LLM_PROVIDER=openai`、代理地址、占位 key `workbench-proxy` 与裁判模型；代理使用随机空闲端口，账本写在输出目录的 `verify_ledger.jsonl`，缓存写在 `verify_llm_cache/`，同一次验证再跑一遍时直接返回缓存的裁判结论。
  - 输出：`awm verify` 自己写出 `verify.<mode>.json`；包装命令打印摘要，包括 `reward_type`、裁判结论与裁判调用次数，日志写在 `verify.<mode>.log`。
  - 占位 key 统一为 `workbench.synth.proxy.PLACEHOLDER_KEY`，合成流水线与本命令共用。
  - 测试见 `tests/integration/test_verify_real_awm.py`：真实的 `awm verify` 子进程，启动环境中植入 key，由 verifier 代码报告它能看到哪些变量。
- 后果：
  - 只有通过 `workbench verify` 运行才有隔离，直接运行 `awm verify` 仍会把 shell 里的 key 交给 verifier 代码；
  - sql 模式下 verifier 代码仍能看到代理地址和占位 key，可以经代理发起 LLM 调用；这些调用记入 `verify_ledger.jsonl`，但本命令没有预算上限；
  - 与 ADR-017 相同，只隔离环境变量，同一用户可读的文件仍然可读。

## ADR-024 训练进程只拿白名单中的环境变量（`train_env`）

- 背景：
  - `workbench train launch` 与 preflight 的探针在 train 环境中启动进程。训练进程运行第三方 ML 代码，还把模型输出交给工具执行：smoke 配置使用 AgentFly 的 calculator 工具，它用 sympy 的 `sympify` 解析模型输出，而 `sympify` 内部使用 `eval`（sympy 1.14.0，`sympy/core/sympify.py:138-139`）。继承 workbench 的全部环境变量，这些代码就能读到所有 key。
  - 训练栈读取的环境变量很多，漏掉一个必需的变量训练就会失败。按源码检索到的变量都是可选的，有默认值（docs/UPSTREAM.md §7.1）：
    - AgentFly @ `1256586`：`XDG_CACHE_HOME`、`AGENT_DATA_DIR`、`AGENT_CONFIG_DIR`、`TOOL_ERROR_AS_OBSERVATION`（`agentfly/__init__.py:16-47`），`REWARD_DECOMPOSITION`、`REWARD_DECOMPOSITION_GAMMA`（`agents/agent_base.py:155-171`），只用于容器工具的 `AGENTFLY_RAY_GET_DEFAULT_TIMEOUT_SEC`；它自己设置 `TOKENIZERS_PARALLELISM` 与 `VLLM_CONFIGURE_LOGGING`；
    - veRL fork @ `001f000`：`VERL_*`、`NCCL_*`、`CUDA_*`、`TORCH_*`、`CUBLAS_WORKSPACE_CONFIG`、`FLASH_ATTENTION_DETERMINISTIC`、`TOKENIZERS_PARALLELISM`、`OMP_NUM_THREADS`；`RANK`、`WORLD_SIZE`、`MASTER_ADDR` 等分布式变量由 Ray 与 veRL 为 worker 设置；跟踪器变量（`WANDB_*`、`MLFLOW_*`、`SWANLAB_API_KEY`、`VOLC_SECRET_ACCESS_KEY`）只在启用对应 logger 时读取，smoke 配置只用 console logger；
    - vLLM、Ray、Hugging Face、Triton、PyTorch 各有一族以固定前缀命名的设置；NVIDIA 容器镜像通常用 `LD_LIBRARY_PATH` 指向驱动库。
- 决定：按名称加前缀放行，前缀之内仍拦下名字像凭据的变量；只按名称放行太容易漏掉训练栈需要的变量。实现为 `train_env()`，由 `src/workbench/train/launch.py`、`src/workbench/train/preflight.py` 使用，配置在 `src/workbench/config.py` 与 `configs/app.yaml`。
  - 放行：`BASE_VARS`（ADR-017）；`NETWORK_VARS`，下载模型可能需要代理与 CA 设置；`TRAIN_VARS`，包括 `HOME` 等缓存位置、`LD_LIBRARY_PATH`、`CC`/`CXX`、线程数与确定性开关、uv 与 AgentFly 的设置；`TRAIN_PREFIXES` 之下、名字不含 KEY、TOKEN、SECRET、PASSW、CREDENTIAL、AUTH 的变量。前缀有 `CUDA_`、`NVIDIA_`、`NCCL_`、`GLOO_`、`TORCH_`、`PYTORCH_`、`TORCHINDUCTOR_`、`TRITON_`、`VLLM_`、`RAY_`、`VERL_`、`HF_`、`HUGGINGFACE_`、`TRANSFORMERS_`、`AGENTFLY_`。
  - 其它变量一律不传，包括 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`HF_TOKEN`、跟踪器的 key，以及云与 git 的凭据。
  - `train.env_passthrough` 按名称额外放行，名字像凭据的也可以，例如某台机器确实需要 `HF_TOKEN`；默认为空。
  - `launch` 与 preflight 的所有探针使用同一个环境，preflight 检查的就是训练会看到的环境；没有显式传入环境时，两者也默认用 `train_env()`。
  - dry-run 输出与运行目录中的 `env_names.json` 列出放行和拦下的变量名，不含变量值。
  - 测试见 `tests/unit/test_train_env.py`，用真实子进程检查。
- 后果：
  - 按前缀放行比只按名称放行宽，前缀之内名字不像凭据的变量都会传下去；
  - 白名单来自源码检索，没有逐个核对 vLLM、Ray 等库读取的全部变量；某台机器需要的变量不在其中时训练会报错，把它加到 `train.env_passthrough` 即可，dry-run 列出的变量名可以用来排查；
  - 与 ADR-017 相同，只隔离环境变量；`HOME` 放行后，训练进程仍能读到该用户可读的文件，例如 `huggingface-cli login` 保存的 token 文件或 `~/.netrc`。

## ADR-025 flash-attn 的构建环境使用锁定版本的 torch（`match-runtime`）

- 背景：
  - flash-attn 2.8.3.post1 在 PyPI 上只有源码包，构建时 import torch 编译 CUDA 扩展（flash-attn `setup.py:22-23`），编出的扩展只能配合同一版本的 torch 使用；`setup.py` 还按构建环境中 torch 的版本去 GitHub 找预编译 wheel（`:440-472`）。
  - AgentFly 的写法是 `[tool.uv.extra-build-dependencies] flash-attn = ["torch"]`（`third_party/AgentFly/pyproject.toml:98-99`）。uv 单独解析这种额外构建依赖，不参考锁文件：uv 0.8.17 上的最小实验中，运行时锁定 `six==1.16.0`，额外构建依赖写成 `"six"` 时装进构建环境的是最新的 1.17.0，写成 `{ requirement = "six", match-runtime = true }` 时是锁定的 1.16.0（docs/UPSTREAM.md §8）。
  - 于是 flash-attn 会对着 PyPI 上更新的 torch 编译，例如依赖 CUDA 13.0 `cuda-toolkit` 的 torch 2.14.0：机器上的 nvcc 是 12.x 时，torch 的扩展构建因 CUDA 主版本不同而报错；即使编出来，也与运行时的 torch 2.10.0 二进制不兼容，要到 import 时才失败。
- 决定：本仓库的 `train/pyproject.toml` 把这项构建依赖写成 `{ requirement = "torch", match-runtime = true }`，构建环境使用锁文件中的 torch 2.10.0；不改 AgentFly。uv 只读取项目根目录的 `[tool.uv]` 设置，路径依赖自己的 `[tool.uv]` 不生效，所以不需要给上游打补丁。不采用先手动装 torch 再以 `--no-build-isolation` 构建的做法：步骤多，而且绕开了锁文件。改动后 `cd train && uv lock --check` 通过，`train/uv.lock` 不变。
- 后果：
  - `extra-build-dependencies` 在 uv 中仍是实验特性，uv 0.8.17 会打印 warning，行为可能随 uv 版本变化，所以 docs/DEPLOYMENT.md 固定使用 uv 0.8.17；
  - 构建环境仍要从 PyPI 取得 torch 2.10.0，与运行环境共用 uv 缓存，编译时间不变。

## ADR-026 写操作审批前在影子环境预演（approval preview）

- 背景：
  - 审批人只看到工具名与参数，看不到调用会改动哪些行。AWM 生成的代码自己提交事务，没有回滚入口，所以不能在会话自己的 server 上执行后回滚，那样也会写真实数据库。
  - LangGraph 恢复被 `interrupt` 暂停的节点时整个节点从头重跑（docs/UPSTREAM.md §10.1），在 approve 节点里、`interrupt` 之前预演会执行两次。
  - 可复用的部件：env-manager 以独立进程组、租用端口、白名单环境变量启动 AWM server（ADR-004、ADR-017）；`envs/snapshot` 用 SQLite 在线备份做快照，`diff` 列出变化的主键；网关的一次性 HMAC 令牌（ADR-006）与 JSONL 审计。
- 决定：write 与 destructive 调用进入审批前，由 env-manager 在隔离端口起一个影子环境，从会话当前数据库的副本启动，执行同一调用后回收；"将要改动的行"附在审批请求上，令牌绑定预演结果，批准并真实执行后比对实际改动。
  - 影子环境（`EnvManager.preview`）：
    - 用 SQLite 在线备份复制会话当前的 `work.db`，会话的 server 照常运行；
    - 用与会话环境相同的启动路径起 AWM server，`--db_path`、`--temp_server_path`、`--output_dir` 全部指向 `<run_dir>/previews/<id>/`，AWM 只写这些位置（docs/UPSTREAM.md §6.2）；
    - 经 MCP 执行同一调用，用 `workbench.envs.changes` 计算改动；
    - 无论成功、出错还是超时，都在 `finally` 中按进程组回收、释放端口、删除目录，回收不受取消影响；
    - 受 `approval.preview_timeout_s` 限制；不占 `max_envs` 名额，并发数受 `env.max_previews` 限制；
    - 经 `EnvService` 同时提供本地实现与 HTTP 实现（`/envs/{sid}/preview`、`/checkpoints`），docker compose 的 env-manager 容器中同样可用。
  - 行级改动与结构比对（`workbench.envs.changes`）：
    - 按 `snapshot.diff` 的方式对行加键，记录新增、删除、修改的行，以及每个修改行改动的列名；展示时每类最多 `approval.preview_max_rows` 行，比对时用全部键；
    - 只比对结构：改动的表，新增、删除、修改的主键，改动的列名；
    - 时间类列只记录、不比对，识别依据是声明类型含 DATE/TIME、时间默认值、`*_at`/`*_time`/`*_date` 等列名；主键不是 INTEGER rowid 别名的表，新增行只比对行数；依据见 docs/UPSTREAM.md §10.3；
    - 每次比对都写明忽略了哪些列（`ignored_columns`）、哪些表按行数比对（`count_only`）。
  - 网关：
    - `Gateway.preview` 生成预演记录，用审批密钥签名，覆盖 id、会话、工具、参数摘要、状态与 digest，digest 是改动的 sha256；同时写一条审计；
    - `issue_approval` 只信任本网关为同一调用签发、且 digest 与内容一致的记录，令牌写入 `p`（预演 digest 或 `preview_unavailable`）与 `pid`；
    - `approval.require_preview` 按风险级别配置，默认 `{write: false, destructive: true}`：为 true 而预演没有成功时不签发令牌，只能拒绝；为 false 时令牌绑定 `preview_unavailable`；
    - 经批准的调用前后各取一次改动（`checkpoint` / `changes_since`），与令牌绑定的预演比对，结果 `preview_check` 写入审计与调用结果：`match`、`preview_mismatch`（附差异与忽略的列）、`preview_unavailable` 或 `check_failed`；
    - 管理端有 `/admin/previews`；`/admin/approvals` 没有给出 `preview_id` 时，先在本请求中预演。
  - 智能体与 API：act 与 approve 之间有单独的 preview 节点，记录保存在 checkpoint 中，approve 节点重跑时不会再预演；批准时把记录交给 `issue_approval`；必需的预演失败时改为拒绝并发出 `approval_refused`，API 在恢复图之前就返回 409。
  - UI：审批卡片显示"将要改动的行"，或醒目的"未预演"与原因；必需的预演失败时禁用 Approve；时间线与对话标出 `preview_mismatch`。
  - 超时默认 30 秒：`scripts/measure_preview.py` 在开发机上测得官方 `e_commerce_33` 的单次预演最慢约 6 秒，其中启动影子 server 占绝大部分，调用本身约 100 ms；取最慢一次的 5 倍，向上取整到 5 秒。
  - 测试见 `tests/unit/test_changes.py`、`tests/unit/test_gateway_preview.py`、`tests/unit/test_env_service.py`、`tests/unit/test_api.py`、`tests/integration/test_preview_real_awm.py` 与 `tests/integration/test_official_data.py`；docker-smoke 经 env-manager 容器断言审批请求带预演 diff、批准后为 `match`；`scripts/demo_ui_check.py` 在浏览器中走通审批卡片。
- 后果：
  - 每个需要审批的调用多等一次影子 server 启动，约数秒，还要多占一个 server 进程的内存，受 `max_previews` 限制；
  - 生成的代码被多执行一次，写数据库以外的副作用（外部请求等）会发生两次；官方数据中这类代码极少（docs/UPSTREAM.md §10.3）；
  - 比对只看结构，时间列与生成的主键无法按值比对；某个 server 如果用随机值给 INTEGER 主键赋值，会被误报为 `preview_mismatch`，误报方向安全；
  - 预演记录用审批密钥验证：进程重启且没有固定 `WORKBENCH_APPROVAL_SECRET` 时，挂起中、要求预演的审批只能拒绝后重新发起；
  - 独立运行的 `workbench gateway serve` 没有 env-manager，无法预演，destructive 调用因此不能在那里批准。

## ADR-027 可配置的审批策略（approval policy）

- 背景：
  - 同为 write，风险可能差别很大，例如加购 1 件与加购 100 件；同为 destructive，删除购物车条目与删除已保存的支付方式也不同。只按风险级别决定是否审批太粗。
  - 判定必须在网关执行：外部 MCP 客户端直连网关时绕过智能体（ADR-005）。
  - 策略写错的代价高，文件要在加载时严格校验。
- 决定：单独的策略文件 `configs/approval_policy.yaml`，由网关在每次调用时判定，智能体只是在调用前向网关问同一个答案。不扩展 `tool_policy.yaml`：它与风险分级混在一起，难以做严格的 schema 校验；也不交给智能体判断：外部客户端会绕过它。
  - 文件与校验（`workbench.gateway.approval_policy`）：pydantic 模型、`extra="forbid"`；未知字段、类型错误、空列表、重复的规则编号、版本不对，在加载时一次列出，每条带 YAML 路径和规则编号；网关启动时按 `approval.policy_file` 加载，文件有错则无法启动。
  - 匹配：规则按文件顺序尝试，第一条命中的规则决定，决策为 `auto_approve`、`require_human` 或 `deny`。可用字段：
    - `tools`、`scenarios`：工具名、场景名的 glob，用 `fnmatch.fnmatchcase` 整名匹配、区分大小写（docs/UPSTREAM.md §10.4）；
    - `risk`：风险级别列表；
    - `args`：参数条件，`lt`/`lte`/`gt`/`gte`/`eq`/`ne` 比较数字，`in`/`not_in` 判断是否在列表中，同一参数的多个条件都要成立。
  - 从严：缺少参数时条件不成立；参数存在但无法比较时，例如数字条件遇到字符串 `"2"` 或布尔值、列表中没有同类型的值，deny 与 require_human 规则视为命中，auto_approve 规则视为不命中，不确定时决策只会更严；字符串形式的数字不转换。
  - 默认：没有命中规则时，write 与 destructive 需要人工审批；read 不需要，除非 `tool_policy.yaml` 的 `require_approval` 列出了 read。`require_approval` 只能增加审批，免除 write 与 destructive 的审批只能写 auto_approve 规则（`policy.needs_approval_by_default`）。
  - destructive 永远不能自动批准，三层保护都在代码中，不依赖配置：
    - 加载时拒绝在同一条规则里同时写 `destructive` 与 auto_approve；
    - `evaluate` 对 destructive 调用跳过 auto_approve 规则并继续匹配，后面的 deny 仍然生效，跳过记为 `guard`；
    - 网关代表策略签发令牌之前再查一次风险级别，即使策略对象回答 auto_approve，destructive 调用也只能等人工审批。
  - 网关执行（`Gateway.call_tool`，在允许清单之后、令牌检查之前）：
    - deny：拒绝，decision 为 `denied_by_rule`，带着令牌也拒绝；`issue_approval` 与 `/admin/approvals` 同样拒绝，返回 409，不先预演；
    - auto_approve：调用方没有给令牌时，网关自己签发一次性令牌，批准人记为 `policy:<规则编号>`，绑定 `preview_unavailable`，然后走已批准调用的路径：真实调用前后各取一次改动，`preview_check` 为 `preview_unavailable`，审计写明由哪条规则自动批准并附上实际改动。自动批准不预演：规则已经决定放行，预演只会多一次影子 server 启动。对本来就不需要审批的 read 调用，auto_approve 等于放行；
    - require_human：需要人签发的令牌；read 调用也可以被规则要求人工审批；
    - 判定结果 `policy`（decision、rule、reason、guard）写入 CallOutcome 与审计，rule 为空表示用了默认。
  - 智能体与 API：act 节点调用 `Gateway.approval_verdict` 得到同一个答案，只有 require_human 进入 preview → approve，auto_approve、deny 与普通 read 直接交给网关；被规则要求人工审批的 read 调用不预演，因为它不改动任何行；审批请求（interrupt 负载、`GET /approvals`）带 `policy`。
  - UI：审批卡片显示"审批策略"一行，即命中的规则编号与说明，或 default；时间线的工具调用显示规则徽标；自动批准的调用显示 "auto-approved by the approval policy"、中性的 "no preview" 徽标和实际改动；被规则拒绝的调用显示 `denied_by_rule`，对话中以警告显示。
  - `workbench gateway policy test` 不运行任何东西，给出某次调用会命中哪条规则、得到什么决策：逐条列出每条规则是否命中及原因、destructive 保护是否生效，并说明该决策在运行时意味着什么；风险级别由 `--risk` 指定，或按 `gateway export-risk` 的方式离线分级。
  - 默认策略不自动批准任何调用：`configs/approval_policy.yaml` 只有两条规则，禁止删除已保存的支付方式（deny），单次加购超过 5 件交人工（require_human）；"官方 e-commerce 场景（`e_commerce_*`）中单次加购不超过 2 件自动批准"（`small-cart-add-official`）只作为注释掉的示例留在文件里。理由：不改配置就部署的人应当得到最严格的行为，放宽由管理员显式决定；官方环境向购物车加入不存在的 offer ID 也会成功写入，网关只能判为 `ok`（ADR-007），这类错误只能靠审批与 DB diff 暴露，而自动批准的调用既不预演也没有人确认。
  - 演示策略 `configs/approval_policy.demo.yaml` 三种决策各一例，即上面两条规则加上 `small-cart-add-official`：
    - `workbench api serve --demo`（`make demo-mock`）默认用它，进程环境设置了 `WORKBENCH_APPROVAL__POLICY_FILE` 时以环境变量为准；docker-smoke 写入的 `.env` 也选择它；
    - 迷你演示场景的加购不命中其中任何规则，演示仍走审批卡片与预演；
    - `GET /healthz` 的 `approval_policy` 字段给出当前生效的文件，以及每条规则的编号与决策。
  - 测试见 `tests/unit/test_approval_policy.py`（包括默认策略没有 auto_approve 规则、把示例取消注释后恰好等于演示策略）、`tests/unit/test_gateway_approval_policy.py`、`tests/unit/test_agent_approval_policy.py`、`tests/unit/test_cli.py` 与 `tests/unit/test_api.py`。
- 后果：
  - 规则只看参数，不看数据库状态，例如购物车总额；
  - auto_approve 的调用执行前没有预演，只能事后比对实际改动；
  - 策略只在启动时加载，改文件要重启；字符串形式的数字不转换，本该自动批准的调用可能交给人工，方向安全；
  - 默认策略下每次 write 都要人工审批，省不了人力；要省，管理员需权衡上面的风险后自行开启 auto_approve 规则；
  - `policy test` 离线分级只看工具名与路由的 HTTP 方法，在线会话还看工具描述，两者可能不同，可以用 `--risk` 指定。

## ADR-028 批量实验：场景级划分、verifier 判定、配对比较

- 背景：
  - ADR-013 不让应用层统计效果，因为小规模自建任务与临时的比较撑不起结论，并说明回答这类问题需要固定模型、固定任务集、足够样本与预先定好的指标。
  - 官方 AgentWorldModel-1K 有上千个场景，大部分任务带有官方生成的 pure-code verifier，工作台已经能隔离地运行任意场景并经 `workbench verify` 判定；具备了按这些要求做实验的条件。
- 决定：新增 `workbench lab` 与 `scripts/lab/`，实验设计在运行前写定（docs/EXPERIMENTS.md）：
  - 被测对象是"模型 + 本工作台"的整体，任务、verifier 与数据只来自官方数据集，固定修订；
  - 按场景划分 train / val / test，训练数据只来自 train 场景，checkpoint 只在 val 上选，每个变体在 test 上只评一次；
  - 基座用非思考模型 Qwen3-4B-Instruct-2507：训练文本与推理时的请求逐字一致（训练脚本逐轮检查并记录），上下文足够长，不因超长中断；
  - 提高结论确定性的设计都不碰 test：test 600 个任务以提高配对检验的功效；val 200 个任务、只看非平凡任务选 checkpoint；训练数据去掉平凡任务；教师解出的 train 任务不够时自动加跑备用任务；报告另列不经 verifier 的过程指标；
  - 成功只由官方 verifier 判定（`reward_type` 为 `complete`），不用模型自评；
  - verifier 本身会误判：先用一个从不回答的模型跑一遍 test，记下什么都不做也被判为 complete 的任务，报告另给去掉这些任务后的成功率与配对比较；
  - 比较都在同一批任务上配对：McNemar 精确检验与配对 bootstrap 区间，单个比率附 Wilson 区间，pass@k 用无偏估计；
  - 训练数据的变体（教师蒸馏、加入学生自身成功轨迹的拒绝采样，以及可选的不过滤消融）用同一套训练与选择流程，差别只在数据；不过滤的变体与教师蒸馏变体的 episode 数相同，比较的只是"是否经过 verifier 过滤"。一次 LoRA 训练在单卡上要数小时，消融默认不跑；
  - 注入实验只包装网关的返回，不改动网关，三种配置的差别只在策略文件与审批方；任务只取自含 destructive 工具的 test 场景（官方数据中约三分之一的场景有这类工具），否则大部分 episode 无从布置注入；
  - 运行记录、adapter 与训练数据只写到 `data/lab/`（不入库），报告的每个数字都从运行记录计算；一次完整运行生成的 `REPORT.md` 与 `summary.json` 原样提交到 `results/lab/`，README 与 docs 引用实验数字时，`make check-numbers` 要求它出现在这份报告中，且页面链接到报告；
  - GPU 侧（vLLM、LoRA 训练、scikit-learn）用 `data/lab/.venv-gpu`，版本与 train 环境的锁一致但不装 AgentFly 与 veRL：实验不用它们的训练循环，少装一个需要编译 flash-attn 的栈。
- 后果：
  - 结论只适用于这一数据集、这一个学生模型与教师模型，且每个配置只有一个种子；
  - verifier 本身会误判，报告中的成功率是"被 verifier 判为 complete"的比例；
  - 批量运行用 `configs/lab/eval_policy.yaml` 自动批准 write，与默认策略（不自动批准任何调用）不同，只用于无人值守的实验；
  - 教师调用 DeepSeek API 产生费用，token 用量逐条记录在运行结果中。

