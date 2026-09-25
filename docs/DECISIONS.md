# DECISIONS — 架构决策记录（ADR）

每条 ADR 包含：背景、可选方案、决定、代价。编号按提出顺序递增，不重排。

---

## ADR-001 上游以 git submodule 引用，而不是把代码拷进来

- **背景**：本仓库要在 AWM、AgentFly 之上做工程化，但不能改变被评测对象（R2），也不能改上游文件（R5）。另外，AWM 没有许可证（UPSTREAM §3）。
- **可选方案**：
  1. 把上游代码 vendoring 进 `src/`；
  2. 通过 PyPI 安装（AWM 未发布到 PyPI）；
  3. 以 git submodule 固定到 commit SHA。
- **决定**：选方案 3。`third_party/` 下的子模块都固定 SHA，并设 `shallow = true`。需要改动时，只能通过 `patches/` 加 ADR 的方式进行。
- **代价**：
  - 克隆后要多执行一步 `git submodule update --init`，doctor 会检查；
  - 运行 AWM 时 Python 会往子模块里写 `__pycache__`。为此统一设置 `PYTHONPYCACHEPREFIX`，已写入 Makefile、CI 和 conftest；
  - 升级上游必须显式修改 SHA 并重新侦察。

## ADR-002 拆成 app 与 train 两个独立的 uv 环境

- **背景**：AWM 要求 `numpy>=2.4.2`，veRL fork 要求 `numpy<2.0.0`；vLLM 0.19 绑定 `torch==2.10.0`。详见 RECON §7。
- **可选方案**：
  1. 单一环境，在中间取一个折中版本（等于修改上游固定版本，违反 R11）；
  2. 两个环境。
- **决定**：选方案 2。
  - 根目录是 app 环境，额外用 `constraint-dependencies` 把关键包锁在 AWM `uv.lock` 的版本上；
  - `train/` 是独立环境，完全按 AgentFly 的固定版本解析，只 lock 不安装。
  - app 只能以子进程方式调用 train（`workbench train ...`）。
- **代价**：两套 lock 要分别维护；app 与 train 之间只能通过文件和子进程交互。

## ADR-003 AWM 没有许可证时的使用方式

- **背景**：Phase 0 发现 AWM 仓库没有任何许可证（UPSTREAM §3），这触发了 R6 停止条件。仓库主人回复"继续"，没有在两个方案之间选择，因此采用 Phase 0 报告中推荐的默认方案 A。
- **可选方案**：
  - A：只以 submodule 指针引用、在运行时调用，不复制、不分发、不打补丁；
  - B：暂停，等上游补上许可证；
  - C：换用 OpenEnv（BSD-3）的环境实现。它同样依赖 AWM 生成的数据，并不能解决问题。
- **决定**：选 A。
  - 本仓库不包含任何 AWM 代码副本，`patches/` 保持为空；
  - README 与 UPSTREAM 显著声明"AWM 未声明许可证，本仓库不对其代码授予任何权利"；
  - 手写的迷你夹具只借用 AWM 的数据格式，不含 AWM 代码。
  - **Docker 镜像**（2026-09-25 补充，仓库主人决定 D20）：`Dockerfile` 构建时把 `third_party/agent-world-model` 复制进镜像，镜像因此内含 AWM 代码。由于 AWM 目前没有许可证，镜像只用于本地和 CI 构建，**不得推送到任何公开或私有的镜像仓库**。2026-09-25 检查过全部工作流，没有推送镜像的步骤（UPSTREAM §3.1）。
- **代价**：法律上仍存在灰色地带，由仓库主人承担判断。一旦上游补上许可证，需要回来更新 UPSTREAM。

## ADR-004 环境服务端以子进程方式运行，建库则直接调用 Python 接口

- **背景**：Phase 2 要求优先调用 AWM 的 Python 接口。但 `awm.core.server.run_server` 内部用 `os.system("python server.py | tee log")` 阻塞运行（`awm/core/server.py:163`），无法在进程内托管。
- **可选方案**：
  1. 在进程内 import 生成的服务代码，自己用 uvicorn 启动。这等于重写 AWM 的补丁逻辑（`server.py:92-143`），会偏离上游行为；
  2. 用子进程运行 `python -m awm.core.server`；
  3. 全部走 `awm` CLI。
- **决定**：
  - 建库调用 Python 接口 `awm.core.reset.reset_single_database`（`reset.py:53-100`）；
  - 服务端用子进程 `python -m awm.core.server`，并且必须显式传 `--db_path`、`--temp_server_path`、`--output_dir`，避免 AWM 往数据集目录写临时文件（`server.py:134-138`）；
  - 子进程放在独立的进程组里（`start_new_session=True`），停止时对整个进程组执行 `killpg`。
- **代价**：
  - 每个环境都有一次 Python 冷启动开销；
  - 只杀 launcher 进程会留下孤儿（Phase 2 已实测），所以必须按进程组管理；
  - 容器里 PID 1 不回收僵尸进程的问题，交给 compose 的 `init: true` 处理。

## ADR-005 网关独立做成一个 MCP server

- **背景**：智能体、外部 MCP 客户端、UI 都需要调用各场景的工具，而且治理规则必须统一：策略、审批、限流、审计、错误规范化。
- **可选方案**：
  1. 把治理逻辑写进智能体代码；
  2. 做成一个普通 REST 服务；
  3. 做成 MCP server（Streamable HTTP），对外仍然是标准的 `list_tools` / `call_tool`。
- **决定**：选方案 3。
  - 核心逻辑放在 `gateway/core.py`，是一个进程内可直接调用的 Python API。本仓库的 LangGraph 智能体直接调用它，省掉一次网络往返。
  - `gateway/server.py` 把同一个核心暴露为 MCP server，任何 MCP 客户端都能接入。
  - 工具名加 `<scenario>__` 前缀，避免不同场景之间冲突；会话通过 `X-Workbench-Session` 请求头识别。
- **代价**：
  - 需要额外维护会话注册，以及审批令牌的传递方式（`X-Approval-Token` 请求头）；
  - MCP 的 `list_tools` 是按会话返回的，客户端必须带上会话请求头。

## ADR-006 默认拒绝（deny-first）

- **背景**：AWM 场景里有大量写操作和破坏性操作，工具清单又是在运行时从上游拉取的。
- **可选方案**：
  1. 默认允许，再按黑名单拦截；
  2. 默认拒绝，按白名单放行。
- **决定**：选方案 2。
  - 会话注册时一次性枚举出允许的工具，之后上游新出现的工具仍然被拒绝。
  - `read` 直接放行；`write` / `destructive` 需要应用层签发的一次性 HMAC 令牌。令牌绑定会话、工具和参数摘要，用一次就作废。哪些风险级别需要审批可以在配置里调整。
  - 无法识别动词的工具，默认按 `write` 处理。
- **代价**：
  - 动词启发式会有误判。例如 `submit_order…` 会落到默认值 `write`，而实际更接近破坏性操作。因此提供了 `overrides` 和 `export-risk` 导出，供人工审核后修正。
  - 每次写操作都需要一次人工确认。

## ADR-007 严格区分"空结果"与"错误"

- **背景**：
  - AWM 生成的环境代码禁止写错误处理，出错时一律是 HTTP 500，经 fastapi-mcp 转成 `isError` 文本（RECON §1.2）；
  - 查询没有结果时返回的是 `isError=False` 且内容为 `[]`（RECON §10）；
  - 如果把两者混为一谈，模型会把"没找到"误当成"失败"而反复重试，或者反过来。
- **决定**：
  - `errors.normalize` 输出三态 `ok` / `empty` / `error`，从不静默返回空；
  - `error` 带结构化信息：`code`、`hints`、`retryable`、`details`，其中包括枚举参数的合法取值和缺失字段；
  - 网关的 MCP 前端用 `validate_input=False`，让参数错误也经过同一套规范化。
- **代价**：规范化依赖对上游错误文本格式的匹配。上游格式一旦变化，就会退化成通用的 `upstream_error`，而不会误判为成功。

## ADR-008 LLM 调用采用两层超时

- **背景**：流式响应可能处于"半开"状态，也就是连接一直不断，但服务端每隔一小段时间只挤出一点数据。httpx 的 read timeout 计的是两次读之间的间隔，每收到一点数据就会重新计时，永远不会触发。
- **可选方案**：
  1. 只设 read timeout；
  2. 只设整体超时；
  3. 两层都设。
- **决定**：两层都设。
  - 第一层：httpx 分相位超时（connect / read / write / pool），尽快发现连不上和完全不响应的情况，这类错误可以重试；
  - 第二层：`LLMClient.chat` 外层用 `asyncio.timeout(total_timeout_s)` 设整体墙钟上限，把包括重试在内的整次调用都框住。
  - 重试只针对网络错误和 5xx，采用带抖动的指数退避；4xx（包括 429）不重试。
- **代价**：墙钟上限必须按"最长的正常回答"来配置，设短了会误杀长回答。已用假服务器验证过两种情况：慢响应会触发读超时并重试；半开连接由墙钟上限截断（`tests/unit/test_llm_client.py`）。

## ADR-009 工具清单与描述在运行时从 `list_tools` 拉取

- **背景**：AWM 各场景的工具由生成代码决定（RECON §1.2），不同场景之间差别很大，同一场景重新合成后也可能变化。
- **可选方案**：
  1. 在 prompt 里手写工具说明；
  2. 每个会话开始时从网关拉取工具清单和描述，再渲染进 prompt 和 LLM 的 `tools` 参数。
- **决定**：选方案 2。
  - intake 节点调用 `gateway.list_tools(session)`，拿到的清单已经是经过白名单过滤、带有风险等级的版本；
  - prompt 文件里只写行为规则，不出现任何具体工具名。
- **代价**：每次会话开始都要多一次 MCP 往返；prompt 的长度随工具数量增长。

## ADR-010 长期记忆按来源设写入门槛

- **背景**：模型的推断如果被写进长期记忆，会在以后的会话里被当成"事实"反复使用，从而放大错误。
- **可选方案**：
  1. 什么都存；
  2. 什么都不存；
  3. 每条事实带来源标记，只接受可以追溯的来源。
- **决定**：选方案 3。
  - 每条记忆带 `user_stated` / `tool_result` / `inferred` 标记，`inferred` 一律拒绝写入，拒绝事件记入 trace；
  - 条目设 TTL，提供 `GET /memory` 和 `DELETE /memory/{key}`；
  - 底层存储用 LangGraph 的 `SqliteStore`。
- **代价**：
  - 来源标记由模型自己填写，门槛只能拦住它自己承认是推断的内容，无法防止模型谎报来源；
  - `SqliteStore` 的过期判断只精确到秒（RECON §10 Phase 5）。

## ADR-011 自合成环境与官方数据严格隔离

- **背景**：R2 禁止改动 AgentWorldModel-1K。此外，AWM 的 `gen scenario` 会把结果写回输入的种子文件（`scenario.py:642`），`env start` 默认会往数据集目录写临时文件（`server.py:134-138`）。
- **可选方案**：
  1. 直接在 `data/awm1k/` 或 AWM 的 `outputs/` 里跑合成；
  2. 每次运行使用独立目录，放在 `data/synth/<run_id>/`。
- **决定**：选方案 2。
  - 输出目录必须位于 `data/synth/` 之下，否则直接报错；
  - 种子文件先复制进运行目录再使用；
  - `manifest.json` 标记 `origin: local-synth`、`official_data: false`；
  - 默认只做 dry-run，只有显式加 `--execute` 并且 LLM 相关环境变量齐全才真正执行。
  - LLM 请求经本地代理转发，顺带实现缓存、重试和按步骤记账。真实 API key 只由代理持有，AWM 子进程拿到的是占位 key。
- **代价**：多了一层本地代理；AWM 自身的重试和代理的重试会叠加。

## ADR-012 不创建 `paper_mirror`；smoke 使用 AgentFly 内置的工具和奖励

- **背景**：Phase 0 结论为 (b)，没有可以镜像的官方配方。AgentFly 的工具和奖励靠 import 时注册，在 Ray worker 中加载外部插件的行为，在没有 GPU 的情况下无法验证。
- **可选方案**：
  1. 按论文文字自己"复刻"一个配方（R2 与任务书明令禁止）；
  2. 写一个 AWM 工具和奖励插件（不可验证，容易变成伪装可用，违反 R8）；
  3. smoke 只演示"rollout → 奖励 → 更新"，使用 AgentFly 自带的 `calculator` 工具和 `math_equal_reward_tool`。
- **决定**：选方案 3，并且不创建 `paper_mirror.yaml`。smoke 配置的 Hydra 键跟随固定 fork 的实际配置结构，并由 preflight 校验。把 AWM 环境接入训练的方案记在 `docs/IDEAS.md`。
- **代价**：smoke 不接触 AWM 环境，只能证明训练链路能打通，与 AWM 本身无关。这一点在 LIMITATIONS 中有说明。

## ADR-013 不做任何效果评测

- **背景**：本仓库的定位是"怎么用、怎么部署、怎么管、怎么看"，被评测对象（Arctic-AWM 模型、AgentWorldModel-1K 数据与官方评测 harness）都来自上游。任务书 R1、R3 禁止跑评测和汇总比率。另外，本开发沙箱没有 GPU，也拿不到 HF 上的模型权重。
- **可选方案**：
  1. 在官方 harness 上复现论文数字（R1 禁止；需要 GPU 与评测 harness，违反 `mcp-adapted-bench` 永不使用的约定）；
  2. 自建一小批任务，统计本应用层（网关、审批、守卫）带来的"效果变化"（R3 禁止；样本量和实验设计都不足以支撑结论，容易误导）；
  3. 不做效果评测：论文数字只登记在 `results/registry.yaml` 并带来源与免责声明；应用层只描述机制，用单元测试和集成测试证明"机制按设计工作"，而不是"模型因此更好"。
- **决定**：选方案 3。`make check-numbers` 扫描 README 与 docs，拦截未登记的百分比、两位小数分数与 Pass@k 数字，以及应用层效果措辞。`awm agent` / `awm verify` 最多各跑一次单任务，只用来证明链路打通。
- **代价**：仓库无法回答"用了网关或审批之后任务完成得更好吗"。这个问题需要独立的实验设计（固定模型、固定任务集、足够样本、预注册指标），记在 `docs/IDEAS.md`，不在本仓库范围内。

## ADR-014 按官方数据修正"空结果"的判定（修订 ADR-007）

- **背景**：
  - ADR-007 认为"查询没有结果时返回 `[]`"，这个判断来自手写夹具，而不是官方数据。
  - 2026-09-24 接入官方 AgentWorldModel-1K 后，在真实启动的 `e_commerce_33` 上实测：无结果的查询返回 `{"products": [], "total": 0}`，空购物车返回 `{"cart_id": 1, "items": []}`。按原规则它们会被判为 `ok`。
  - 同时观察到一种新的参数错误文本：`Input validation error: 'abc' is not of type 'integer'`（`docs/verification/2026-09-24-dataset.md`）。
- **可选方案**：
  1. 维持原规则，让模型自己解读包装对象（与 ADR-007 的初衷相悖）；
  2. 为每个工具维护"哪个字段是结果列表"的配置（1000 个场景无法人工维护）；
  3. 通用规则：对象中至少有一个列表字段、所有列表字段都为空、且没有非空的嵌套对象时，判为 `empty`；计数、ID、分页等标量字段不影响判定。
- **决定**：选方案 3，实现在 `gateway/errors.py` 的 `is_empty_payload`。另外为"类型不符"的校验错误补充 `details.expected_type` 与提示。单元测试覆盖这些官方形态，`official_data` 集成测试在真实官方环境上验证。
- **代价**：
  - 这是启发式规则。例如 `{"success": false}` 与 `get_product_by_id` 对不存在 ID 返回的占位对象（`id: 0`）仍判为 `ok`，网关无法通用地识别；
  - 上游生成代码对不存在的外键也会直接写入（例如向购物车加入不存在的 offer），这类语义错误只能靠审批与 DB diff 暴露，网关不做判断。
- **补充（同日，复查写操作）**：
  - 问题：上述规则不区分读写。在官方 `social_media_4` 上实测，`patch_hidden_subreddits` 移除全部隐藏项后返回 `{"user_id": 1, "hide_subreddit_ids": [], ...}`，数据库确实被修改，却被判为 `empty`。对官方 1000 个环境的静态扫描显示，写类路由（POST / PUT / PATCH / DELETE）中约有 1647 条的返回模型只含列表与标量字段，都可能被这样误判（`docs/verification/2026-09-24-empty-on-writes.md`）。
  - 影响：智能体没有按 `empty` 分支的硬编码流程；但 act 提示词把 `empty` 解释为"没有匹配结果"，模型可能误以为写操作没有生效而重试；审计日志与指标也会把成功的写操作记为 `empty`。"无状态变化"守卫使用数据库指纹，不受影响。
  - 决定：`empty` 只用于风险级别为 `read` 的工具；`write` / `destructive` 工具调用成功时一律为 `ok`（`normalize(..., read_only=...)`，由网关按分级传入）。
  - 剩余代价：名称里带读动词、实际会写入的工具（例如 `get_or_create_active_cart`）仍按 `read` 处理；可以通过 `configs/tool_policy.yaml` 的 `overrides` 纠正。
  - 更正（Phase 12.5）：上句的例子不准确，`get_or_create_active_cart` 含动词 `create`，实际判为 `write`。真正受影响的例子见 ADR-015（例如 `DELETE purge_my_list_by_maturity_level`）；ADR-015 已用 HTTP 方法下限处理这类工具。

## ADR-015 风险分级以路由的 HTTP 方法为下限（修订 ADR-006）

- **背景**：
  - ADR-006 的动词启发式只看工具名和描述的第一个词。2026-09-24 对官方数据集的静态扫描发现，18374 个 POST/PUT/PATCH/DELETE 工具中有 38 个按名称被判为 `read`：名词 `list`、`view` 被当成读动词（例如 `DELETE purge_my_list_by_maturity_level`、`POST record_answer_view`）；运行时描述首词 "Get" 还会把 `POST ensure_direct_dm_with_user` 判成 `read`。
  - 被判为 `read` 的工具不需要审批（ADR-006），成功时还可能被标成 `empty`（ADR-014）。
- **可选方案**：
  1. 扩充动词表或加停用词：名词与动词同形（list、view），治标不治本；
  2. 运行时读取环境服务的 `/openapi.json`：精确，但依赖运行中的服务；
  3. 用离线目录（`gen_envs.jsonl` 的 `full_code`）中路由的 HTTP 方法作为风险下限。
- **决定**：选方案 3（仓库主人在 Phase 12.5 指定）。
  - `envs/catalog.route_methods` 用 `ast` 解析 `@<obj>.<method>(...)` 装饰器，不执行代码。工具名取 `operation_id`；没有 `operation_id` 时按 FastAPI 0.115.12 的 `generate_unique_id` 推导（`fastapi/utils.py:179-184`，OpenAPI 使用它：`fastapi/openapi/utils.py:237`），单测与 FastAPI 实际生成的 OpenAPI 逐项比对。fastapi-mcp 0.4.0 以 operationId 作为工具名（`fastapi_mcp/openapi/convert.py:50-63`）。同一 operationId 出现在多个路由上时，取风险最高的方法。
  - 下限：DELETE → `destructive`；POST / PUT / PATCH → `write`；GET 和其它方法不设下限。`classify(..., http_method=...)` 先算启发式，再抬到下限；来源记为 `http_method`，理由里保留启发式原来的结论。
  - 数据流：env-manager 启动会话时读取该场景的方法表（与 AWM 一样，同名场景取最后一条记录：`awm/core/server.py:98-99`），经 `EnvInfo` 交给网关注册会话；远程 env-manager（docker compose）的 HTTP 接口同样返回它。独立网关的 `POST /admin/sessions` 可选传 `tool_methods`。`export-risk` 离线导出时同样使用方法表，并新增 `http_method` 与 `heuristic_risk` 两列。官方数据与 local-synth 的 `gen_envs.jsonl` 都由 AWM 生成，同样适用。
  - 目录里查不到的工具（场景不在数据目录中、代码无法解析、路径或 `operation_id` 不是字面量）沿用原来的启发式与保守默认值。
  - `overrides` 是人工审核后的决定，按原样生效，即使低于下限（例如把以 POST 实现的纯查询设为 `read`）；导出表中的 `http_method` 列可以暴露这类覆盖。
- **结果**（工程事实，由脚本统计，`docs/verification/logs/2026-09-24-phase12.5-risk-floor.log`）：官方 35062 个工具中，POST/PUT/PATCH/DELETE 共 18374 个；其中被判为 `read` 的从修复前代码导出的 38 个降到 0 个。级别变化：37 个 `read` → `write`，1 个 `read` → `destructive`，55 个 `write` → `destructive`。新增 38 个需要审批的工具，没有工具因此不再需要审批。
- **代价**：
  - 以 POST 实现的纯查询（例如 `get_trip_price_quote`）现在需要审批，人工确认变多；需要时用 `overrides` 调低。
  - 用 GET 实现的写操作仍然识别不了：方法与语义不一致时，下限帮不上忙。
  - 每次启动会话要多读一次 `gen_envs.jsonl`；官方 104 MB 的文件上实测约 0.1 s（只对目标场景解码 JSON 并解析代码）。

## ADR-016 审计脱敏不得把时间戳当成电话号码

- **背景**：Phase 12 的真实运行中，网关审计摘要把 `2026-09-24T17:05:31.506430` 写成了 `2026-09-24T17:05:[PHONE]`（`docs/verification/logs/2026-09-24-phase12-workbench-agent.log`）。原来的 `PHONE` 正则只要求匹配不紧跟在单词字符之后；`31.506430` 前面是冒号，于是被当成一个 8 位的电话号码。复查还发现，空格分隔的时间戳（官方数据库行中的 `2026-09-23 17:05:16`）会变成 `[PHONE]:05:16`。
- **可选方案**：
  1. 先用单独的时间戳正则找出所有时间戳区间，再跳过与其重叠的电话匹配；
  2. 收紧 `PHONE` 正则本身，排除时间戳的上下文。
- **决定**：选方案 2（仓库主人指定"收紧 PHONE 正则"）。在原正则上加三条限制：匹配不得从单词字符或小数点之后开始（`(?<![\w.])`）；不得紧跟在"数字:"之后开始，排除秒与微秒；不得紧接在":数字"之前结束，排除"日期 小时"。纯日期 `YYYY-MM-DD` 仍由 `_mask_phone` 保留。实现在 `gateway/audit.py`。
- **验证**：单测覆盖 6 种时间戳写法原样保留，10 种电话写法仍被脱敏（包括 `tel:+1…`、国家码、括号、点号分隔、与时间戳同在一行）；Phase 12 真实工具结果的审计摘要中时间戳保持原样。
- **代价**：紧跟在"数字:"之后、或后面紧接":数字"的电话号码不再被脱敏（例如 `ext1:5551234567`）；非 ISO 格式的日期（例如 `24.09.2026`）仍可能被当成电话号码。脱敏仍是启发式的，只作用于审计摘要，trace 不做脱敏。

## ADR-017 按 DeepSeek 文档回传 reasoning_content（只改 LLM 层）

- **背景**：
  - DeepSeek 默认开启思考模式。思考模式指南（`https://api-docs.deepseek.com/guides/thinking_mode`，中文版 `https://api-docs.deepseek.com/zh-cn/guides/thinking_mode`，2026-09-24 18:00 UTC 用 curl 读取）原文：
    > In thinking mode, the chain-of-thought content is returned via the reasoning_content parameter, at the same level as content.
    > If the request carries the tools parameter: the reasoning_content of all previous turns should be passed back to the API and will be concatenated into the context.
    > If the request does not carry the tools parameter: reasoning_content does not need to be passed back; even if passed to the API, it will be ignored
    > for requests carrying the tools parameter, the reasoning_content must be fully passed back to the API in all subsequent requests — even for turns where the model did not perform a tool call. If your code does not correctly pass back reasoning_content, the API will return a 400 error.
  - 流式响应里是 `delta.reasoning_content`（API 参考 `https://api-docs.deepseek.com/api/create-chat-completion`，同时读取）。
  - Phase 12 时本仓库既不保留也不回传它。当时实测 DeepSeek 没有返回 400（探针 P1、P3），但不能依赖这一点。
- **约束**：仓库主人要求只改 `src/workbench/llm/`，不加 `extra_body` 开关。act 节点自己构造历史消息，只保存 `content` 和 `tool_calls`，LLM 层拿不到智能体状态。
- **决定**：
  - 后端从流式的 `delta.reasoning_content` 或非流式的 `message.reasoning_content` 读取思维链，放进 `ChatResult.reasoning_content`，智能体不使用它。
  - 新增 `llm/reasoning.py` 的 `ReasoningStore`：每得到一个带 `reasoning_content` 的回复，就以"之前的非 system 消息 + 该回复的 content 与第一个工具调用 id"为键记下来。之后的请求**只要携带 `tools`**，就给其中能认出的每条 assistant 消息补上 `reasoning_content`（复制消息，不修改调用方的字典）；不带 `tools` 的请求不补，因为文档说会被忽略。调用方自己带了 `reasoning_content` 的消息保持原样。
  - 是否回传只取决于服务端有没有返回过 `reasoning_content`，不按 URL 判断，也不加开关。不返回它的服务端（一般的 OpenAI 兼容服务、未启用 reasoning parser 的 vLLM）收到的请求体与原来完全相同。
  - 识别时不看 system 消息：重新规划会重建 system prompt，而其余历史保持不变。
- **验证**：单测用一个按文档行为的假服务端，`strict` 模式下对缺少 `reasoning_content` 的带 `tools` 请求返回 400。真实的智能体图对它完整跑通；把回传关掉（负对照）后同一测试失败。其它单测覆盖：流式与非流式读取、只对带 `tools` 的请求回传、没有工具调用的回答轮次也回传、重新规划后仍能回传、未知消息和调用方自带的值不改动、不返回 reasoning 的服务端不会多出字段、容量上限。
- **文档没写清、因而没有猜测的情况**（记入 LIMITATIONS）：
  - 没有 `reasoning_content` 的历史轮次该怎么传：非思考模式产生的、来自其它后端的、进程重启后丢失的。本仓库原样发送，不补空值；
  - API 参考只把请求消息中的 `reasoning_content` 描述为 Chat Prefix Completion（Beta）的输入，与思考模式指南的要求不一致。本仓库按思考模式指南实现。
- **代价**：
  - 记录只存在进程内存中：服务重启后从 checkpoint 恢复的会话，历史轮次的 `reasoning_content` 已丢失，按文档可能得到 400，智能体以 `llm_error` 终止；
  - 每个后端实例最多记 1024 条，按 LRU 淘汰，极长或大量并发的对话可能丢掉较早轮次；
  - 两个会话的非 system 历史完全相同时（例如同一请求、同一回答、还没有工具调用）共用一条记录；
  - 多个工具调用的轮次只按第一个调用 id 识别，因为 act 节点只保留第一个调用。

## ADR-018 act 调用只经原生 `tools` 参数传工具定义；按实测重设预算默认值

- **背景**：act 节点把完整的工具定义放了两遍：system prompt 里的 `tools_block`（每个工具的描述与参数 schema），以及请求的原生 `tools` 参数。Phase 12 在官方 `e_commerce_33`（39 个工具）上实测每次 act 调用约 26K token，默认 `agent.token_budget=60000` 会在第 3 次 act 之前终止，演示时只能用环境变量调高预算。
- **可选方案**：
  1. 只保留 system prompt 里的定义、不发原生 `tools`：会失去原生工具调用，DeepSeek 等服务端也不再能按 schema 生成调用；
  2. 只经原生 `tools` 传定义，system prompt 只保留工具名与风险级别的简表（仓库主人在 Phase 12.5 指定）。
- **决定**：方案 2。
  - `agent/nodes/common.py` 的 `act_system_prompt` 组合 act 提示词、计划和 `tools_risk_table`（每个工具一行：名称、风险级别、是否需要审批）；描述与 schema 只出现在原生 `tools` 参数中。plan 请求不带 `tools` 参数，仍用 `tools_block` 提供完整定义，未改。
  - act 提示词升到 v2，加一句"描述与参数 schema 随请求的 tools 提供，下表只列名称与风险级别"，记入 CHANGELOG。
- **测量**（离线，不调用 API，`docs/verification/logs/2026-09-24-phase12.5-act-tokens.log`）：
  - 分词器与编码：`deepseek-ai/DeepSeek-V4.1-Flash` @ `dba1be0` 的 `tokenizer.json` 与官方编码脚本 `encoding/encoding.py`（MIT）。用它重算 Phase 12 四个探针的请求，与 DeepSeek 实际计费的 `prompt_tokens`（286、13248、309、40）逐一相同，差值为 0。
  - 同一次 act 调用（官方 `e_commerce_33` 任务 0，Phase 12 的计划，思考模式）：修复前 25640 token，修复后 14358 token，每次少 11282 token；其中原生 `tools` 本身 13231 token。
  - 按 Phase 12 的轨迹推算，同一次运行的总量从 121954 token 降到约 76826 token。
- **新的默认值**（代码与 `configs/app.yaml` 同步）：
  - `agent.token_budget`：60000 → **240000**。依据：39 个工具的官方场景（数据集平均每个场景约 35 个工具）上跑满 `max_steps`=12 次 act 仍在预算内，也就是先触发步数上限而不是预算：plan 12627 + Σ_{k=0..11}(14482 + 659k) + verify 1804 = 231709，向上取整。其中 14482 是修复后的第一次 act（Phase 12 实测 25764 减去 11282），659 是 Phase 12 中相邻 act 调用 token 数的平均增长。预算仍然是成本保护：工具更多或工具结果更长时会先触发它。
  - `llm.max_tokens`：2048 → **8192**。依据：Phase 12 思考模式下每次调用的输出（含思考 token，按"计费总量 − 离线 prompt"得到的上界）为 plan 376、act 124/178/784/960、verify 250，最大 960；取其 8 倍向上到 2 的幂。思考长度随任务难度变化，DeepSeek 在思考模式下的默认上限是 64K；该上限只在模型真的输出那么多时才计费。
- **代价**：
  - 模型在 system prompt 里看不到工具描述，只能从原生 `tools` 读取，因此服务端必须把 `tools` 渲染进提示词。DeepSeek 会这样做（见上面的离线编码）。对 vLLM v0.19.0 读源码发现：请求带 `tools` 却没有 `tool_choice` 时默认为 `"auto"`（`vllm/entrypoints/openai/chat_completion/protocol.py:640-643`），服务端未配置 tool parser 时直接返回错误 `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set`（`vllm/entrypoints/serve/render/serving.py:197-215`）；配置之后 `tools` 才会交给 chat template 渲染（同文件 `:223-247`）。act 请求一直带原生 `tools`（修复前也是），所以按当前的 serving profile（未设 parser）vLLM 后端本来就会在第一次 act 调用时失败，与本 ADR 无关；记入 LIMITATIONS U1，未在 GPU 上验证；
  - 预算默认值只按一个参考场景与一次运行测得，不代表所有场景；
  - plan 请求仍带完整定义（约 12K token），未在本 ADR 范围内。

## ADR-019 执行生成代码的子进程只拿到白名单中的环境变量（deny-first）

- **背景**：env-manager 用 `dict(os.environ)` 启动 AWM env server（修复前在 `src/workbench/envs/manager.py:215`），而 server 执行的是场景中由 LLM 生成的 `full_code`（`awm/core/server.py:163`）。`SynthRunner.validate` 与 `workbench synth validate` 同样把完整环境交给 `awm env reset_db`（执行生成的 SQL）和 `awm env check_all`。后者逐个启动生成的 server，而 `awm/core/env.py:161-172` 的 `Popen` 没有传 `env=`，所以这些 server 会继承它的环境。gen 步骤则是在完整环境上叠加代理设置。结果是 `DEEPSEEK_API_KEY`，以及容器里的云令牌和 git 令牌，生成代码都能读到。
- **可选方案**：
  1. 黑名单：删除名字像密钥的变量（`*_API_KEY`、`*_TOKEN` 等）。名字不规则的秘密会漏掉，例如 `GIT_CONFIG_VALUE_0` 中的认证头、代理 URL 中的凭据；
  2. 白名单（deny-first）：只传运行必需的变量，其余一律丢弃。这是仓库主人在 Phase 12.5 指定的方案。
- **决定**：采用方案 2，由新模块 `src/workbench/subprocess_env.py` 实现。
  - **不调用 LLM 的子进程**用 `generated_code_env()`，它只保留 `BASE_VARS`：`PATH`、`LANG`、`LANGUAGE`、`LC_ALL`、`LC_CTYPE`、`TZ`、`TMPDIR`、`PYTHONIOENCODING`、`PYTHONUTF8`、`PYTHONUNBUFFERED`、`PYTHONPYCACHEPREFIX`。`PYTHONPYCACHEPREFIX` 缺省时补为 `.cache/pycache`。
    - 使用者：env server（`envs/manager.py`）、`SynthRunner.validate` 与 `workbench synth validate`（reset_db 和 check_all）。
    - 这些子进程都不调用 LLM，所以拿不到任何 key。
  - **gen 步骤**：这些步骤都调用 LLM，其中 `gen env` 与 `gen verifier` 还会执行自己生成的代码（`awm/core/env.py:161-172`、`awm/core/verifier.py:104`）。`SynthRunner.step_env` 只在白名单之上加 LLM 设置：
    - 经本地代理时（`workbench synth run --execute` 总是经代理），只加占位 key `workbench-proxy`、代理地址和 `AWM_SYN_OVERRIDE_MODEL`；上游 key 留在 workbench 进程中的代理线程里。步骤只连本机代理，所以也不传出站代理变量。
    - 不经代理时（只能在 Python 中直接调用 `execute(proxy_base=None)`），加 `LLM_VARS` 与 `NETWORK_VARS`：
      - `LLM_VARS`：AWM 读取的 LLM 变量（`awm/gpt.py:38-55`、`awm/tools.py:407-432`、`awm/core/scenario.py:63-84`）；
      - `NETWORK_VARS`：出站代理与 CA 设置。
  - **为什么这些变量就够**：
    - `PATH` 必需：AWM 用 `sh` 管道把 server 输出交给 `tee`（`awm/core/server.py:163`）。
    - 解释器是 `sys.executable`，即 venv 中的 python，不依赖 `VIRTUAL_ENV` 或 `PYTHONPATH`。
    - `PORT` 与 `DATABASE_PATH` 由 AWM 启动器自己设置（`awm/core/server.py:157-158`）。
    - 不传 `HOME`：运行不需要它，子进程需要时仍能从 passwd 查到家目录。
  - **附带效果**：以前父进程若设置了 `HOST`，会覆盖 `--host`，因为生成代码读的是 `os.environ.get('HOST', ...)`（`awm/core/server.py:114`）。现在 `--host` 总是生效。
  - **不在范围内**：只执行固定命令的子进程仍继承完整环境，包括 `git rev-parse`、`nvidia-smi` 与 train 环境的版本探针。
- **验证**（2026-09-24，CPU）：
  - `tests/unit/test_subprocess_env.py`：
    - 真实子进程在旧的 `dict(os.environ)` 交接下能看到植入的 `DEEPSEEK_API_KEY`（对照组）；
    - 在白名单下看不到任何 `*_API_KEY` 或其它像密钥的名字，也看不到植入的值和白名单外的变量；
    - gen 步骤只有占位 key，没有出站代理变量；
    - reset_db 与 check_all 没有任何 key；`synth validate` 命令也一样。
  - `tests/unit/test_env_manager.py::test_env_server_process_gets_no_secrets`：检查 env-manager 启动的进程。
  - `tests/integration/test_env_real_awm.py::test_env_server_process_tree_sees_no_secrets`：
    - 对真实 AWM 启动器、`sh`、`tee` 与生成的 server 所在的进程组，逐个读取 `/proc/<pid>/environ`；
    - 同时确认服务仍然健康，7 个工具都在；
    - 这些进程里另有三个非密钥变量：`PORT`、`DATABASE_PATH`，以及启动器 import scikit-learn 时设置的两个 `KMP_*`（`sklearn/__init__.py:56,60`，经由 `mcp_agent/workflows/embedding/embedding_base.py:6`）。
  - **负对照**：把 manager 临时改回 `dict(os.environ)` 后，上面两条测试都失败。
  - 白名单环境下，真实的 `awm env reset_db` + `check_all`（`tests/integration/test_synth_validate_real_awm.py`）和官方场景的启动测试（`official_data`）都通过。
- **代价与限制**：
  - 白名单固定，没有配置开关（按要求不加功能）。某个部署如果确实需要额外变量，只能改代码。
  - 只隔离环境变量：子进程与 workbench 以同一用户运行，仍能读取该用户可读的文件，例如 `.env`、`~/.aws/credentials`。要做到这一点需要另一个用户或容器沙箱，本 ADR 未做。
  - `gen env` 与 `gen verifier` 的生成代码：
    - 经代理时仍能看到占位 key 和代理地址，可以经代理发起 LLM 调用（会产生费用，并记入账本），但拿不到上游 key；
    - 不经代理时会看到真实的 `OPENAI_API_KEY`。
    - 不改上游就无法把这两个步骤的"调用 LLM"和"执行生成代码"分开。
  - `awm verify` 无法隔离：它在同一进程里执行 verifier 代码（`awm/core/verify.py:104-126`、`:151-174`，namespace 中直接提供了 `os`），又用环境中的 key 调用裁判（`:230-302`、`:419-421`）。不改上游就无法隔离，见 LIMITATIONS。（2026-09-25 起由 ADR-025 的 `workbench verify` 解决。）
  - `awm agent --scenario` 自动起服时，server 继承 `awm agent` 的环境（`awm/core/server.py:174-195` 的 `Popen` 没有传 `env=`）。本仓库只用 `--mcp_url` 模式连接 env-manager 启动的 server。

## ADR-020 Arctic-AWM 的 vLLM serving profile 启用 `hermes` tool parser

- **背景**：
  - 智能体的 act 请求一直带原生 `tools`。按 vLLM v0.19.0 源码，这样的请求在服务端没有开启 `--enable-auto-tool-choice` 和 `--tool-call-parser` 时会被直接拒绝（ADR-018，LIMITATIONS U1）。
  - 模型卡没有指定 parser，所以此前的 serving profile 两项都没有设置。
  - 仓库主人在 Phase 12.5 报告之后决定启用 `hermes`，只改 `configs/serving/arctic-awm-4b.yaml` 与 serve 脚本（`user-decisions.md` D11）。
- **依据**（都是源码事实，文件:行号见 RECON "Phase 12.5 报告之后"一节）：
  - **模板要求的格式**：`Snowflake/Arctic-AWM-4B` @ `437dfa0` 的 `chat_template.jinja`（md5 `da05f6b8…`，与 2026-09-24 读取的模型卡一致）：
    - 带 `tools` 时，system prompt 要求模型输出 `<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>`（第 11 行）；
    - 历史中的工具调用也按同一格式渲染（第 65-73 行）；
    - `tokenizer_config.json` 中 `<tool_call>` 与 `</tool_call>` 是 id 151657 与 151658 的 added token，`special: false`，所以默认解码（`skip_special_tokens=True`）不会把它们删掉。
  - **`hermes` parser 解析的格式**（vLLM v0.19.0 `vllm/tool_parsers/hermes_tool_parser.py`）：
    - 非流式：用正则 `<tool_call>(.*?)</tool_call>|<tool_call>(.*)` 取出标签之间的文本（`:61-66`），按 JSON 读取 `name` 与 `arguments`，`arguments` 再序列化成字符串（`:106-126`）；第一个 `<tool_call>` 之前的文本作为 `content`（`:128`）。
    - 流式：同样按标签切分，用 `"name"` 与 `"arguments"` 取值（`:160-208`）。
    - 这与上面的模板格式一致。`hermes` 已在 `vllm/tool_parsers/__init__.py:61-64` 注册；该版本的 parser 只做文本匹配，不要求这两个标签在词表中。
  - **不带 `tools` 的请求不受影响**：
    - **`tool_choice` 保持 `"none"`**：`ChatCompletionRequest.tool_choice` 默认为 `"none"`（`chat_completion/protocol.py:175-181`），只有带 `tools` 时才改成 `"auto"`（`:642-643`）。
    - **预处理不调用 parser**：`tool_choice` 为 `"none"` 时，预处理不调用 parser 的 `adjust_request`（`serve/render/serving.py:534-550`）。
    - **非流式不解析**：自动解析的条件是 `tool_choice` 为 `"auto"` 或 `None`（`engine/serving.py:920-924`），所以不会解析，消息原样返回 `content`（`chat_completion/serving.py:1476-1479`）。
    - **流式不创建 parser**：流式要求 `request.tools` 非空才创建 parser（`chat_completion/serving.py:532-535`、`:559-571`、`:1752-1757`）。
    - **`awm agent` 正是这种请求**：它只发 `model`、`messages`、`max_completion_tokens`、`temperature` 和 vLLM 的 `extra_body`，不带 `tools`，也不带 `tool_choice`（`awm/core/agent.py:367-381`）。所以它的 `<tool_call>` 文本协议照旧留在 `content` 里，由 AWM 自己解析（`awm/core/agent.py:130-167`）。
  - **workbench 的处理**：`openai_compat` 后端（`vllm` 后端复用它）收到原生 `tool_calls` 时直接使用，并从 `content` 中去掉 `<think>`；没有原生调用时再从正文解析 `<tool_call>`（`src/workbench/llm/backends/openai_compat.py:187-198`）。启用 parser 后，act 请求得到的是原生调用，不需要改代码。
- **决定**：`configs/serving/arctic-awm-4b.yaml` 设 `enable_auto_tool_choice: true`、`tool_call_parser: hermes`；`reasoning_parser` 与 `chat_template` 仍不设置。`workbench serve vllm-cmd` 与 `scripts/serve_vllm.sh` 从 profile 生成参数，现在包含 `--enable-auto-tool-choice --tool-call-parser hermes`。serve 脚本只更新了注释。
- **代价与未验证项（UNVERIFIED-LOCAL，留到 Phase 15 在 GPU 上验证）**：
  - 以上都是读源码得到的结论，没有在真实服务上运行过。
  - 没有设置 reasoning parser，思考内容（`<think>…</think>`）留在 `content` 中，由 workbench 客户端去掉。如果模型在思考内容里写出了 `<tool_call>` 标签，parser 会把它当成工具调用；`qwen3` reasoning parser 可以把思考内容分出去，但不在 D11 的范围内，也未验证。
  - `docker-compose.yml` 的 `vllm` 服务命令是写死的，不读 profile。按 D11 当时只改了 profile 与 serve 脚本。2026-09-24 仓库主人授权同步（`user-decisions.md` D16）：compose 的命令现在与 `workbench serve vllm-cmd` 的输出一致，只有 `--host` 为 `0.0.0.0`，以便其它容器访问；同时补上了 `--served-model-name`。`tests/unit/test_llm_client.py::test_compose_vllm_matches_the_serving_profile` 断言两者一致，防止再次漂移。

## ADR-021 合成流水线可以从 `gen task` 开始（`--scenario-file`）

- **背景**：
  - Phase 13 要真实执行合成流水线，但 `gen scenario` 必须有 embedding 端点：`awm/core/scenario.py:63` 断言 `EMBEDDING_OPENAI_API_KEY` 存在，随后用它建 embedding 客户端（`:83-86`）。
  - 本轮唯一的 LLM 服务 DeepSeek 没有 embedding 接口（2026-09-24 查阅）：
    - API 文档站点地图中的 9 个 API 页面都与 embeddings 无关；
    - `GET /models` 只返回 `deepseek-flash` 与 `deepseek-v4-pro`。
  - TASK_v2 Phase 13 第 1 步与仓库主人的决定 D5 要求：跳过场景生成，手写 1 条企业类场景（名称加 `local_` 前缀，格式与官方 `gen_scenario.jsonl` 一致），从 `gen task` 开始；先读源码确认 `awm gen task --input` 能直接以场景文件为输入，不支持就停下。
- **源码确认**：上游 CLI 支持这样做。
  - `awm gen task` 的 `input` 字段注释就是 "scenario description file"（`awm/core/task.py:13`），CLI 帮助中它是必填项。
  - `run` 直接加载这个文件（`:126-129`），每条只读取 `name` 与 `description`（`:44-45`）。
  - 官方 `gen_scenario.jsonl` 的 1000 条恰好都只有这两个字符串字段。
- **可选方案**：
  1. 手工写 `state.json`，把 `scenario` 步骤标成已完成。这等于绕过编排层，而任务书禁止"自己绕过"；manifest 也不会记录场景来源。
  2. 给 `workbench synth run` 加 `--scenario-file`：从计划中去掉 `gen scenario`，把文件复制为运行目录中的 `gen_scenario.jsonl`，作为 `gen task` 的输入。
- **决定**：采用方案 2，由 `src/workbench/synth/runner.py` 与 `cli.py` 实现。
  - **校验**：
    - 每行必须恰好是 `{"name": str, "description": str}`，与官方格式一致；
    - 名称必须是 AWM 规范化后的形式（`awm/tools.py:335-339`），以 `local_` 开头，并且不以 `_<数字>` 结尾。只靠前缀不够：官方 revision `dde80a0` 的 1000 个场景名全部是 `<类别>_<序号>`，其中 `local_search_1` 与 `local_services_marketplace_1` 也以 `local_` 开头；
    - 官方数据在本地时（`env.dataset_dir` 下的 `gen_scenario.jsonl`），名称不得与其中任何一个相同；manifest 的 `official_name_check` 记录这项检查是否执行（ADR-011）；
    - `--scenarios` 不能超过文件中的条数。
  - **不覆盖已有输入**：运行目录中已有内容不同的 `gen_scenario.jsonl` 时报错；内容相同则继续，以便续跑。
  - **所需环境变量**：只有 `gen scenario` 需要 embedding key，所以这种模式下 `--execute` 只要求 `OPENAI_API_KEY` 与 `AWM_SYN_OVERRIDE_MODEL`。
  - **记录来源**：manifest 记录 `skipped_steps: ["scenario"]`，以及场景文件的路径与 sha256。
  - **默认行为不变**：不传该参数时仍从 `gen scenario` 开始，仍要求 embedding key。
- **代价**：
  - 手写场景代替了 `gen scenario` 的生成与去重，这一步没有执行，也没有得到验证（U9 中 `gen scenario` 的部分仍未验证）。
  - `local_` 前缀与"不以 `_<数字>` 结尾"是本仓库的约定，不是 AWM 的要求；后者依据的是当前官方数据的命名方式。

## ADR-022 合成运行的中断与续跑：每个步骤独立成进程组，未完成的步骤从头重做

- **背景**：
  - Phase 13 的故意中断没有落在 gen 步骤中间（`docs/verification/2026-09-24-synth.md`）：
    - 监视脚本把信号发给了外层包装 shell 的进程组 2548，而运行本身在 `setsid` 建立的进程组 2549；
    - 改为手动 SIGTERM 时，6 个 gen 步骤都已完成，中断落在收尾的 `check_all`；
    - `check_all` 的测试 server 和 `/tmp` 下的临时目录只能手动清理。
  - 旧 runner 用 `subprocess.run` 在自己的进程组里启动步骤，由此产生三个问题：
    - SIGTERM 只发给 runner 时，Python 的默认处理直接结束 runner，步骤进程继续运行；
    - 终端 Ctrl-C 会发给整个前台进程组，runner 与 AWM 各自处理，结果取决于时机；
    - AWM 用 `start_new_session=True` 启动它测试的 server（`awm/core/env.py:161-172`），这些 server 不在步骤的进程组里。只有正常路径上 AWM 自己用 `killpg` 回收它们（`:212-227`），步骤被杀后它们就成了孤儿。
  - 续跑按步骤进行：runner 重做未完成的步骤，已付费的请求由代理缓存原样重放（ADR-011）。问题在于 AWM 各步骤写输出的方式不同：
    - 6 个步骤都在结束时一次覆盖写出，见 `awm/core/scenario.py:629`、`task.py:142`、`db.py:259`、`sample.py:282`、`spec.py:120`、`env.py:568`；
    - `gen verifier` 每处理一批就追加写入（`awm/core/verifier.py:172-176`）。它自带的续跑只重新生成校验不通过的行，新行追加在旧行后面（`:145-156`、`:260-308`、`:456`）；
    - `awm verify` 取第一条匹配的行（`awm/tools.py:456-472`）。
    - 所以 `gen verifier` 中途中断后直接重跑，排在前面的旧的无效行会被 `awm verify` 选中。
- **可选方案**：
  1. 只修正演示时的信号目标，runner 不变；
  2. 由 runner 负责回收与重做：
     - 每个步骤放进独立的进程组；
     - 收到 SIGINT/SIGTERM 时，回收步骤及其全部后代所在的进程组；
     - 重做未完成的步骤前，先移开它的输出。
- **决定**：选方案 2，实现在 `src/workbench/synth/runner.py`、`src/workbench/envs/procs.py` 与 `src/workbench/cli.py`。
  - **启动**：每个步骤以 `start_new_session=True` 启动，stdin 接 `/dev/null`，因此终端的 Ctrl-C 只会到达 runner。
  - **信号**：`execute()` 运行期间把 SIGTERM 转成 `KeyboardInterrupt`，与 SIGINT 走同一条路径。
  - **回收**（`stop_process_tree`）：
    - 趁步骤进程还在，按 `/proc/<pid>/stat` 里的 ppid 建树，找出全部后代；
    - 对这些进程所在的每个进程组先发 SIGTERM，最多等 5 秒，仍有存活的再发 SIGKILL；
    - 永远不向 runner 自己所在的进程组发信号。
  - **状态与退出码**：被中断的步骤记为 `interrupted`，CLI 以退出码 130 结束，用同一条命令续跑。收尾验证（`reset_db`、`check_all`）经同一个 runner 执行，中断时同样回收。
  - **重做前移开输出**：
    - 未完成的步骤重做前，已有的输出移到 `attempts/<步骤>.<n>/`（不删除），`state.json` 中该步记录 `set_aside`；
    - 这样每次尝试都从没有输出的状态开始，结果与一次未中断的运行相同；
    - 已经付费的请求由缓存重放。
- **验证**：零成本，不调用付费 API。
  - 单测 `tests/unit/test_synth_resilience.py`：
    - 用真实子进程，假上游监听真实端口（`tests/unit/synth_harness.py`）；
    - 向 runner 自己的 PID 发 SIGTERM，分别打断一次写出的步骤（`db`）和追加写出的步骤（`verifier`）；
    - 断言中断落在步骤中间：该步已收到 1 个响应，第 2 个请求还在途。假上游在这个请求第一次到达时把它保持 3 秒，所以无论 runner 反应多慢，信号都落在它等待期间。起初窗口只有正常的 0.3 秒上游延迟，CI（ci run 27）上出现过 runner 停下步骤之前第 3 个请求已经发出的情况；在本地给 runner 人为加 0.5 秒反应延迟可以复现，保持请求后加到 1.5 秒也能通过；
    - 断言该步在独立会话里启动的"测试 server"被回收；
    - 断言续跑以 0 退出，每个请求只到达上游一次，中断前收到的响应由缓存命中，输出与未中断的运行相同。
  - 反向对照：
    - 旧 runner 在"server 已回收"这一条断言处失败；
    - 去掉"移开输出"后，`verifier` 用例失败。
  - 用真实 AWM 重做 Phase 13 的中断：见 `docs/verification/2026-09-24-phase14-synth-resilience.md`。
- **代价**：
  - 靠 `/proc` 找后代，只适用于 Linux。步骤一旦退出，它的子进程会被重新挂到别的父进程下，因此必须在步骤还在运行时回收；中断发生时步骤正在运行，满足这个条件。
  - runner 自己被 SIGKILL 时无法回收。
  - AWM 被 SIGTERM 结束时不执行 `finally`，`gen env` 与 `check_all` 的临时目录（`/tmp/env_test_*`，`awm/core/env.py:148`、`:230-235`）会残留，需要手动删除。
  - 移开输出后，AWM 在 `gen env`、`gen verifier` 中自带的续跑不再起作用。请求正文与上次相同时由缓存重放；正文不同的请求会再次计费，并受 ADR-023 的预算约束。

## ADR-023 合成预算熔断：本地代理按账本累计费用拒绝转发

- **背景**：
  - 仓库主人的决定 D17(b)：
    - 本地代理按账本累计费用，超过上限时拒绝转发，并让当前步骤失败；
    - 提高上限后可以续跑；
    - 上限在配置中设置，默认 ¥5。
  - 此前账本只在运行结束时汇总（`ledger_summary.json`），执行过程中没有上限。Phase 13 靠操作者脚本按账本监视，设了 ¥6 止损（LIMITATIONS §6）。
  - 请求被拒绝时 AWM 进程不会失败：
    - openai SDK（2.38.0）把 402 映射为普通的 `APIStatusError`（`openai/_client.py:1081-1112`），SDK 自己也不重试 402（`openai/_base_client.py:795-826`）；
    - AWM 的 `GPTClient` 把它当作一般异常，共尝试 3 次，间隔 3 秒、6 秒（`awm/gpt.py:168-204`）；
    - 仍然失败时，`GPTClient` 返回一个内容为空的 refusal completion（`:205-206`、`:103-131`）。步骤继续执行，可能以 0 退出。
    - 因此 runner 不能只凭退出码判断预算熔断。
- **可选方案**：
  1. runner 只在步骤之间比较累计费用：步骤内部不受限制，单个步骤就可能远超上限；
  2. 代理在每次转发前检查，并记录被拒绝的请求；runner 在步骤开始前和结束后各检查一次。
- **决定**：选方案 2，实现在 `src/workbench/synth/` 下的 `proxy.py`、`ledger.py`、`runner.py`，以及 `src/workbench/config.py`、`src/workbench/cli.py` 与 `configs/app.yaml`。
  - **配置**：`synth.budget`，默认 `5.0`，单位是价格表 `configs/pricing.yaml` 的币种（CNY）。可用环境变量 `WORKBENCH_SYNTH__BUDGET` 覆盖；设为 `null` 时关闭熔断。
  - **计费口径**：与账本汇总相同，只计未命中缓存、也未被拒绝的请求，按价格表计算，是上界口径（`Ledger.spent`）。
  - **代理**：
    - 缓存命中照常返回，不受上限影响，因为它不产生费用；
    - 需要转发到上游的请求先检查。以下情况拒绝转发，返回 HTTP 402（`type: budget_exceeded`），并在账本中记一条 `refused: true`：
      - 已花费不低于上限；
      - 请求的模型不在价格表中，或账本中已有没有价格的模型。这时无法计算费用，所以不放行（fail closed）。
  - **runner**：
    - 每个步骤开始前检查同样的条件；条件成立就不启动该步骤（`SynthBudgetExceeded`），账本不变；
    - 步骤结束后，只要本步有 `refused` 条目，就把该步记为 `failed`、`reason: "budget"`，不看退出码；
    - CLI 以退出码 1 结束，并提示提高 `synth.budget` 后用同一命令续跑。续跑时先移开该步的输出（ADR-022），已付费的请求由缓存重放。
  - **dry-run**：`workbench synth run` 不加 `--execute` 时，输出中的 `budget` 列出上限、币种和已花费。
  - **适用范围**：只在经代理执行时生效；`workbench synth run --execute` 总是经过代理。
- **验证**：`tests/unit/test_synth_resilience.py`，零成本，假上游每次调用计 ¥1。
  - 上限 2.5：
    - `task` 的第 2 个请求被拒绝；
    - 假步骤像 AWM 一样把错误变成空回复并以 0 退出，runner 仍把该步判为失败。
  - 同一上限再运行一次：步骤不启动，账本不变。
  - 上限提高到 100：
    - 续跑成功，第 1 个请求由缓存命中；
    - 含空回复的旧输出被移到 `attempts/task.1/`；
    - 每个请求只付费一次。
  - 代理层单测：
    - 超限后缓存命中仍然可用；
    - 没有价格的模型被拒绝；
    - `budget: null` 时不限制。
- **代价**：
  - **在途超支**：检查发生在转发之前。已经转发、尚未返回的请求照常完成并计费，所以实际花费可能超过上限。
    - 超出部分最多是超限那一刻所有在途请求的费用之和；
    - 在途请求数受 AWM 各步骤的并发设置约束（`GPTClient` 默认 64，`awm/gpt.py:36`）；
    - 按价格表上界和思考模式默认 64K 的输出上限计算，单个请求仅输出部分最多约 ¥0.5。
  - **拒绝后的重试**：AWM 会再试 2 次，所以每个被拒绝的请求在账本中有 3 条 `refused` 记录，步骤也多等约 9 秒。
  - **读账本的开销**：每次转发前都重新读取整本账本，请求数很多时有额外开销。本仓库的运行规模下可以忽略，Phase 13 一共只有 15 个上游响应。
  - **价格表覆盖**：价格表只有 `deepseek-flash`。换用其它模型前（包括 `gen scenario` 需要的 embedding 模型），要先在价格表中加上价格，或显式设 `budget: null`。

## ADR-024 上游错误记入账本，按阈值判定合成步骤（`done_with_failures`）

- **背景**：
  - Phase 14 回放重做中，`gen verifier` 的 50 个请求全部得到 404，AWM 写出 10 行空结果后以 0 退出，runner 仍把这一步记为完成（`docs/verification/2026-09-24-phase14-synth-resilience.md` §3）。原因有两个：
    - AWM 的 `GPTClient` 把失败的请求变成空回复，不抛异常（`awm/gpt.py:168-206`）；
    - 代理只记录成功的响应，runner 只看退出码和输出文件。
  - 仓库主人的决定 D19：
    - 代理把"所有重试后仍以上游错误结束"的请求记入账本，状态为 failed；
    - 新增 `synth.max_failed_requests`，默认 0：
      - 超过阈值时，步骤判失败，可以续跑；
      - 大于 0 但不超过阈值时，步骤记为 `done_with_failures`，并在报告与 validation 输出中列出失败数，不得记为 `done`；
    - 判定逻辑与已有的 refused 检查统一。
  - 同一个请求可能被三层重试，其中后两层的重试在代理看来都是正文相同的新请求：
    1. 代理自己对 5xx 与网络错误重试（`max_retries`）；
    2. openai SDK 2.38.0 对 408、409、429、5xx 重试 2 次（`openai/_base_client.py:795-826`）；
    3. AWM 对任何错误共尝试 3 次（`awm/gpt.py:168-204`）。
- **可选方案**：
  1. 按失败次数计数：AWM 重试 3 次的一个请求会被算成 3 次，之后成功了的请求也会被算作失败；
  2. 按请求正文（缓存键）归并：同一请求的所有尝试算作一个请求，最后一次尝试决定结局，只有最终没有得到回答的请求才算丢失。
- **决定**：选方案 2。实现在 `src/workbench/synth/` 下的 `proxy.py`、`ledger.py`、`runner.py`、`validate.py`，以及 `src/workbench/config.py`、`src/workbench/cli.py`、`configs/app.yaml`。
  - **代理**：
    - 4xx（含 402、429）不重试，原样返回给步骤，同时记一条 `failed: true`，带上游状态码；
    - 5xx 与网络错误在代理重试完之后返回 502，同时记一条 `failed: true`。状态码取最后一次的上游状态；网络错误没有状态码，统计时记作 `network`；
    - 每条账本记录都带请求的缓存键 `key`；
    - 错误响应不是 JSON 时（例如 HTML 错误页），代理把它包装成 JSON 错误返回。此前这种响应会让代理自己出错。
  - **统计**（`step_requests`）：
    - 按 `key` 归并一个步骤写下的账本记录，由最后一次尝试决定结局；
    - 得到两个数：被拒绝的请求数，以及最终失败的请求数（按最后一次的错误分类，即状态码或 `network`）；
    - 没有 `key` 的旧记录各算一个请求。
  - **判定**（`judge_step`，与预算熔断统一）：
    1. 有被拒绝的请求 → `failed`，`reason: budget`（ADR-023），不受阈值影响；
    2. 最终失败的请求数超过 `synth.max_failed_requests` → `failed`，`reason: upstream_errors`。CLI 以退出码 1 结束，提示修复上游或提高阈值后用同一命令续跑；
    3. 退出码非 0 或输出缺失 → `failed`；
    4. 最终失败的请求数在 1 到阈值之间 → `done_with_failures`；
    5. 其余 → `done`。
    - 该步在 `state.json` 中记录 `failed_requests` 与 `failed_by_status`；有被拒绝的请求时记录 `refused_requests`。
  - **续跑**：
    - `done_with_failures` 只在失败数不超过当前阈值时算作完成。调低阈值后用同一命令续跑，会重做这一步；
    - 某一步被重做之后，其后的所有步骤也都重做，因为它们的输入可能已经变了。请求正文不变的，由缓存重放；
    - 此前这个问题不会出现：失败的步骤会让运行停下，它后面的步骤从未完成过。
  - **输出**：
    - CLI 打印的步骤状态里带失败数；`done_with_failures` 的步骤另有一行黄色提示；
    - `ledger_summary.json` 每步新增 `failed_calls`，即失败的尝试次数；
    - `validation.json` 新增 `gen_steps_with_failed_requests`，`validation.md` 列出这些步骤；这对 `workbench synth validate` 同样适用。
  - **配置**：`synth.max_failed_requests` 默认 0，可用 `WORKBENCH_SYNTH__MAX_FAILED_REQUESTS` 覆盖，不能为负数。
- **验证**（零成本，`tests/unit/test_synth_failures.py`，共 19 项）：
  - **代理**：402、429、503、网络错误各一例，检查返回的状态码、重试次数、账本记录、不写缓存；另有一例非 JSON 的错误体。
  - **统计与判定**：
    - 同一请求的多次尝试只算一次；后来成功的请求不算丢失；旧账本各算一个；
    - 判定表 7 行；阈值不能为负。
  - **整次运行**：步骤是真实子进程，代理是真实的，错误由脚本化的 transport 注入：
    - 阈值 0：402 让步骤失败；上游恢复后，同一命令续跑成功；
    - 阈值 1：429 让步骤记为 `done_with_failures`，运行继续；同一阈值再跑，不重做；阈值改为 0 后，重做该步及其后所有步骤；
    - 阈值 1：503 与网络错误各丢失一个请求，共 2 个，超过 1，步骤失败；代理对两者各重试了一次；
    - 步骤自己重试成功的请求不算丢失。
  - **validation 输出**：列出 `done_with_failures` 的步骤。
  - **反向对照**：代理不记录失败时，9 项失败；去掉"重做其后所有步骤"时，阈值用例失败。
- **代价**：
  - 只有经代理执行时才能这样判定。AWM 内部的其它失败（例如解析模型输出失败、生成的代码校验不通过）不是上游错误，不在账本里，仍然只能看 AWM 的退出码与输出。
  - 5xx、网络错误与超时的请求可能已经在上游计费，账本只能按 0 计。
  - 同一步骤里正文完全相同的两个请求，会被当作同一个请求的两次尝试。
  - `done_with_failures` 的步骤输出缺少那些请求的结果，下游步骤照常运行。阈值设为大于 0，就意味着接受不完整的产物。

## ADR-025 `workbench verify`：`awm verify` 不再拿到任何 key

- **背景**：
  - ADR-019 留下的缺口：`awm verify` 在同一个进程里做两件事，不改上游就无法把它们分开：
    - 执行数据集中的 verifier 代码，namespace 里直接提供了 `os`（`awm/core/verify.py:104-126`、`:151-174`）；
    - sql 模式下，从同一进程的环境变量读取裁判的地址与 key（`:416-433` 调用 `resolve_llm_config`，`awm/tools.py:386-437`）。
  - Phase 12 直接在带 key 的 shell 里运行 `awm verify`，verifier 代码能读到 `DEEPSEEK_API_KEY`。
  - 仓库主人的决定 D13(a)、D18：执行 Phase 15 之前，让 `awm verify` 经本地代理运行、只拿占位 key。
- **源码确认**（AWM @ `85e322f`）：
  - 只有 `--mode sql` 会调用 LLM 裁判；`--mode code` 执行确定性的 verifier，直接返回 `complete` 或 `others`（`awm/core/verify.py:416-433`；配置检查也只针对 sql 模式，`:51-80`）。
  - 官方数据集同时提供两种 verifier：`gen_verifier.jsonl`（sql）与 `gen_verifier.pure_code.jsonl`（code），默认路径见 `:376-379`。
- **可选方案**：
  1. 只在文档里要求"先清空环境再运行"：依赖使用者自觉，无法测试；
  2. 新增包装命令：code 模式只给白名单环境；sql 模式启动本地代理，`awm verify` 只拿到代理地址和占位 key，真实的上游地址与 key 留在 workbench 进程里。这与合成流水线的做法相同（ADR-011、ADR-019）。
- **决定**：选方案 2，新增 `workbench verify`（`src/workbench/verify.py`、`src/workbench/cli.py`）。
  - **参数**：
    - `--input`：`awm agent` 的输出目录；
    - `--mode`：`code` 或 `sql`，默认 `code`；
    - `--verifier`：verifier 文件，默认取 `env.dataset_dir` 下对应模式的官方文件；
    - `--init-db`、`--final-db`：初始与最终数据库；
    - `--judge-model`：sql 模式的裁判模型，默认读 `AWM_SYN_OVERRIDE_MODEL`。
  - **code 模式**：子进程只拿到 ADR-019 的白名单（`generated_code_env`），没有任何 key。
  - **sql 模式**：
    - 裁判的上游地址与 key 仍从 workbench 进程的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 读取（与 `synth.upstream_*_env` 相同）；
    - `awm verify` 子进程在白名单之外只拿到四个变量：`AWM_SYN_LLM_PROVIDER=openai`、代理地址、占位 key `workbench-proxy`、裁判模型；
    - 代理使用随机空闲端口，账本写在输出目录的 `verify_ledger.jsonl`，缓存写在 `verify_llm_cache/`。同一次验证再跑一遍时，直接返回缓存的裁判结论，不再调用。
  - **输出**：`awm verify` 自己写出 `verify.<mode>.json`；包装命令打印摘要（`reward_type`、裁判结论、裁判调用次数），日志写在 `verify.<mode>.log`。
  - **其它**：占位 key 改为 `workbench.synth.proxy.PLACEHOLDER_KEY`，合成流水线与本命令共用。
- **验证**（零成本，`tests/integration/test_verify_real_awm.py`，真实的 `awm verify` 子进程）：
  - 测试在启动环境里植入 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`HF_TOKEN`，并让 verifier 代码报告它在 `awm verify` 里能看到什么。
  - **对照组**：按 Phase 12 的方式带完整环境运行 `awm verify`，verifier 代码能看到全部三个植入的变量。
  - **code 模式**：一个也看不到；除白名单外，没有任何变量来自启动环境。
  - **sql 模式**：
    - verifier 代码只看到占位 key；
    - 本地的假裁判收到了代理带上的真实 key 与指定的模型，结论为 `complete`；
    - 再跑一遍时由缓存返回，假裁判没有收到第二次调用。
  - **反向对照**：把包装命令的环境改回完整环境后，code 与 sql 两项测试都失败。
- **代价**：
  - 只有通过 `workbench verify` 运行时才有隔离，直接运行 `awm verify` 仍会把 shell 里的 key 交给 verifier 代码。runbook 与文档都改用本命令。
  - sql 模式下，verifier 代码仍能看到代理地址和占位 key，可以经代理发起 LLM 调用。这些调用会记入 `verify_ledger.jsonl`，但本命令没有预算上限。
  - 与 ADR-019 相同，只隔离环境变量，同一用户可读的文件仍然可读。
