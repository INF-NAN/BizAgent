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
