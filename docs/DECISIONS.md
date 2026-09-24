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

