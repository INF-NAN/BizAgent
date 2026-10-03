# WALKTHROUGH — 学习路线

这份路线面向第一次读这个仓库的工程师，按"环境 → 合成 → 服务 → 网关 → 智能体与 UI → 训练启动器 → 数字纪律 → 批量实验"的顺序展开。每一步写明目标、命令，以及要读的源码和测试。

命令都在仓库根目录执行。`workbench` 安装在 app 环境里，下文省略前缀 `uv run`；`scripts/` 下的 Python 脚本照写 `uv run python …`。除 GPU 上的模型服务、smoke 训练与批量实验的完整运行外，所有步骤都能在 CPU + mock LLM 下运行；GPU 部分见 [docs/DEPLOYMENT.md](DEPLOYMENT.md) 与 [docs/EXPERIMENTS.md](EXPERIMENTS.md)。

先读 [README](../README.md)、[docs/ARCHITECTURE.md](ARCHITECTURE.md)（分层、时序与状态机）、[docs/DECISIONS.md](DECISIONS.md)（ADR）和 [docs/UPSTREAM.md](UPSTREAM.md)（上游版本、许可证与接口要点，精确到上游文件与行号）。

---

## 0. 准备

```bash
make setup     # 初始化两个 submodule，安装 app 环境，生成迷你夹具，检查 train 锁文件
make doctor    # 即 workbench doctor
make test      # 单元与集成测试，只用 mock LLM；make test-unit / make test-integration 分开跑
```

`make setup` 不初始化 AgentFly 里嵌套的 `verl` 子模块，也不安装 train 环境，这两步只在训练机器上做（第 6 节）。

在一台没有 GPU、没有下载官方数据集的机器上，`make doctor` 的输出节选如下：

```
│ python                      │ ok     │ 3.12 (AWM and AgentFly require 3.12)
│ submodule:agent-world-model │ ok     │ pinned at 85e322f69279
│ submodule:AgentFly          │ ok     │ pinned at 1256586b1109
│ dataset                     │ warn   │ data/awm1k: missing gen_tasks.jsonl, gen_db.jsonl, gen_sample.jsonl, gen_envs.jsonl (run `make data`)
│ ports                       │ ok     │ env pool 18100-18199: 0 busy
│ env-vars                    │ ok     │ backend=mock_replay
│ llm                         │ ok     │ mock_replay fixture tests/fixtures/trajectories/e_commerce_33_basic.jsonl
```

缺少数据集和 GPU 只给 warn，退出码为 0；任何一项 fail 时退出码为 1。

`configs/app.yaml` 的每个键都可以用环境变量 `WORKBENCH_<节>__<键>` 覆盖，例如 `WORKBENCH_LLM__BACKEND=vllm`，环境变量优先于文件。

下文默认使用迷你夹具 `tests/fixtures/awm_mini` 中的场景 `mini_e_commerce`。它按 AWM 的数据格式手写，有 7 个工具、2 个任务、6 张表，不是官方数据。7 个工具的名称、参数名与是否必填、响应的顶层字段名，以及其中 5 张表的表名取自官方 `e_commerce_33`，来源记录在 `tests/fixtures/awm_mini/MANIFEST.json`。

`make data` 把官方数据集 AgentWorldModel-1K 下载到 `data/awm1k`（revision `dde80a0`，CC-BY-4.0，不入库）。下载后，把命令里的 `mini_e_commerce` 换成官方场景（例如 `e_commerce_33`）并去掉 `--dataset-dir` 即可。官方 `e_commerce_33` 有 39 个工具，清单见 [docs/examples/e-commerce-33-tools.md](examples/e-commerce-33-tools.md)。依赖官方数据的测试带 `official_data` 标记，没有数据时自动跳过。

## 1. 环境：隔离的 AWM 环境会话

目标：理解"一个会话 = 一份独立的 SQLite + 一个 AWM MCP server 子进程"（ADR-004）。

```bash
export WORKBENCH_ENV__DATASET_DIR=tests/fixtures/awm_mini
workbench env search cart --dataset-dir tests/fixtures/awm_mini   # 离线检索场景目录
workbench env serve &                                              # env-manager 控制面，默认 127.0.0.1:8090
workbench env up mini_e_commerce --session-id wt1
workbench env ls
workbench env snapshot wt1 before
workbench env diff wt1                                             # 默认与 initial 比较；--against before 与快照比较
workbench env restore wt1 initial
workbench env logs wt1 -n 50
workbench env down wt1
```

`env search` 只读数据集目录，不启动任何进程：

```
┃ scenario        ┃ tools ┃ tasks ┃ tables ┃
│ mini_e_commerce │ 7     │ 2     │ 6      │
```

`env up` 经控制面创建会话：在 `data/runs/<session_id>/` 下建一份场景数据库，从 `env.port_min`–`env.port_max` 中租一个端口，在独立进程组中启动 AWM server，健康检查通过后以 JSON 打印会话信息，其中有 MCP 地址和工具列表。`env diff` 按主键列出表级改动。`env down` 用 `killpg` 回收整个进程组。并发上限、排队、启动与空闲超时由 `env` 一节的 `max_envs`、`queue_timeout_s`、`start_timeout_s`、`idle_timeout_s` 控制。

阅读：

- `src/workbench/envs/awm_adapter.py`：建库调用 AWM 的 Python 接口；server 以子进程启动，并且总是显式传 `--db_path`、`--temp_server_path`、`--output_dir`，否则 AWM 会把文件写进数据集目录或 submodule（docs/UPSTREAM.md §6.2）；
- `src/workbench/envs/manager.py`：进程组与 `killpg`、健康检查截止时间、并发信号量与排队、空闲回收；AWM 内部会再起 `sh | tee` 管道，只杀启动进程会留下孤儿 server；
- `src/workbench/subprocess_env.py`：server 执行生成的代码，只拿到白名单中的环境变量（ADR-017，docs/UPSTREAM.md §6.8）；
- `src/workbench/envs/snapshot.py`：快照、恢复、按主键的表级 diff；
- `src/workbench/envs/service.py`、`src/workbench/envs/http.py`：同进程的 `LocalEnvService` 与经 HTTP 访问控制面的 `RemoteEnvService`；
- `src/workbench/envs/ports.py`、`src/workbench/envs/procs.py`、`src/workbench/envs/health.py`；
- 测试：`tests/unit/test_env_manager.py`、`tests/unit/test_env_service.py`、`tests/unit/test_snapshot.py`、`tests/unit/test_subprocess_env.py`、`tests/integration/test_env_real_awm.py`（启动真实 AWM server，并检查整个进程树的环境中没有任何 key）。

## 2. 合成：编排 AWM 的生成流水线

目标：理解如何在不动官方数据的前提下，把 `awm gen` 的各个子步骤组织成可续跑、可记账、可校验的流水线（ADR-011）。

```bash
workbench synth run --scenarios 2 --out data/synth/demo             # 默认 dry-run，只打印计划
workbench synth run --scenarios 1 --out data/synth/demo \
  --scenario-file my_scenarios.jsonl                                # 跳过 gen scenario，从 gen task 开始
workbench synth run --scenarios 2 --out data/synth/demo --execute   # 真正调用 LLM
workbench synth validate data/synth/demo                            # 单独重跑 reset_db 与 check_all
```

dry-run 不创建任何文件或目录。它打印运行目录、`origin`、种子文件、执行所需与缺少的环境变量、预算、`max_failed_requests`，以及 7 个步骤各自的 `awm.cli gen …` 参数与状态。节选：

```
  "mode": "dry-run",
  "origin": "local-synth",
  "seed": "third_party/agent-world-model/outputs/seed_scenario.jsonl (copied into run dir)",
  "required_env": [
    "OPENAI_API_KEY",
    "AWM_SYN_OVERRIDE_MODEL",
    "EMBEDDING_OPENAI_API_KEY"
  ],
  ...
  "budget": {
    "limit": 5.0,
    "currency": "CNY",
    "spent_so_far": 0.0
  },
  "max_failed_requests": 0,
```

隔离与输入：

- 输出只能放在 `data/synth/` 下，manifest 标 `origin: local-synth`。合成的环境不入库，不用于训练，也不与官方数据混合。
- 种子文件先复制进运行目录，因为 `gen scenario` 会把分类结果写回它的输入文件。
- `gen scenario` 需要 embedding 端点。没有时用 `--scenario-file` 从 `gen task` 开始（ADR-019）：文件每行与官方 `gen_scenario.jsonl` 格式相同，只有 `name` 与 `description`；名称以 `local_` 开头，不带 `_<数字>` 后缀，也不能与官方场景重名。这时不再需要 `EMBEDDING_OPENAI_API_KEY`。
- `--execute` 需要 `OPENAI_API_KEY` 与 `AWM_SYN_OVERRIDE_MODEL`；`OPENAI_BASE_URL` 可以指向任何 OpenAI 兼容端点。runner 在本进程内起一个本地 LLM 代理，负责缓存、重试、按步骤记账和预算熔断，真实 key 只留在代理里。gen 步骤只拿到占位 key，`reset_db` 与 `check_all` 不拿任何 key（ADR-017）。

续跑（ADR-020）：

- `state.json` 记录每一步的状态。同一命令再次运行时，跳过已完成且输出齐全的步骤；一旦某一步重跑，它之后的步骤全部重跑。
- 每一步在自己的进程组中运行。Ctrl-C 或 SIGTERM 会停止当前步骤及其启动的所有进程，包括 AWM 在独立会话中启动的测试 server。该步记为 `interrupted`，CLI 以退出码 130 结束。
- 中断或失败的步骤重做前，残留输出先移到 `attempts/<步骤>.<n>/`，不删除任何东西。已经得到回答的请求由代理缓存 `llm_cache/` 重放，不再计费。
- AWM 被信号结束时不清理自己的临时目录，`/tmp/env_test_*` 可能残留，需要手动删除（docs/UPSTREAM.md §6.7）。

预算熔断（ADR-021）：

- 上限是 `synth.budget`，默认 5，按 `configs/pricing.yaml` 计价，币种 CNY。价格表取最高一档，所以账本费用是上界。
- 每一步开始前，runner 比较账本累计费用与上限。达到上限后代理拒绝转发，返回 HTTP 402，缓存命中照常返回；当前步骤失败，CLI 以退出码 1 结束。所用模型在价格表中没有价格时，步骤也不会启动。`synth.budget: null` 关闭熔断。
- 提高上限后用同一命令续跑，例如 `WORKBENCH_SYNTH__BUDGET=8 workbench synth run --scenarios 2 --out data/synth/demo --execute`。dry-run 输出中的 `budget` 显示上限与已花费。

上游错误（ADR-022）：

- AWM 把重试后仍失败的 LLM 请求变成空回复，并以退出码 0 结束，所以 runner 除了看退出码，还按账本判定每一步。
- 代理对 5xx 与网络错误重试，4xx 直接传回；重试后仍以上游错误结束的请求记入账本。
- 一步中这样丢失的请求多于 `synth.max_failed_requests`（默认 0）时，这一步失败。修好上游或调高阈值后，用同一命令续跑。
- 不超过阈值时，这一步记为 `done_with_failures`，失败数列在 CLI 输出与 `validation.json` 中。调低阈值后，同一命令会重做这一步。

阅读：

- `src/workbench/synth/runner.py`：`plan_steps`、`execute`、`judge_step`、`step_env`、`stop_process_tree`、`interruptible`、`_set_aside`、`_budget_block`；
- `src/workbench/synth/proxy.py`（`over_budget`、缓存与重试）、`src/workbench/synth/ledger.py`（`step_requests`、费用汇总）；
- `src/workbench/synth/validate.py`：把 `awm env check_all` 的结果整理成 `validation.json` 与 `validation.md`；
- AWM 各步骤怎样写输出、自带的续跑与 LLM 客户端的重试：docs/UPSTREAM.md §6.7；
- 测试：`tests/unit/test_synth.py`；`tests/unit/test_synth_resilience.py` 与 `tests/unit/test_synth_failures.py` 借助 `tests/unit/synth_harness.py` 起真实子进程和一个本地假上游，覆盖中断续跑、预算熔断与上游错误，不调用任何付费 API；`tests/integration/test_synth_validate_real_awm.py`。

## 3. 服务：模型服务与 LLM 客户端

目标：理解 LLM 客户端的后端抽象、两层超时（ADR-008），以及为什么还要从正文中解析 `<tool_call>`。

后端只由配置切换：`llm.backend` 取 `mock_replay`（默认，回放手写脚本）、`vllm` 或 `openai_compat`。

```bash
workbench serve vllm-cmd     # 只打印 vLLM 命令
workbench serve probe        # 服务起来之后：各发 1 个请求，检查两类工具调用
```

`serve vllm-cmd` 按 `configs/serving/arctic-awm-4b.yaml` 生成命令：

```
vllm serve Snowflake/Arctic-AWM-4B --host 127.0.0.1 --port 8000 --served-model-name Snowflake/Arctic-AWM-4B --gpu-memory-utilization 0.9 --enable-auto-tool-choice --tool-call-parser hermes
```

最后两个参数由 profile 启用（ADR-018）。智能体的 act 请求带原生 `tools`，vLLM 要有 tool parser 才接受这类请求。不带 `tools` 的请求，例如 `awm agent` 发出的请求，不经过 parser，`<tool_call>` 文本原样留在 `content` 里。服务没有返回原生 `tool_calls` 时，客户端从 `content` 中解析 `<tool_call>` 兜底。依据见 docs/UPSTREAM.md §9。

在 GPU 机器上用 `scripts/serve_vllm.sh` 从 train 环境启动服务，或用 `docker compose --profile gpu up vllm` 在容器中启动。然后设置 `WORKBENCH_LLM__BACKEND=vllm`。`workbench serve probe` 默认连 `llm.base_url` 与 `llm.model`，发两个请求：

- `native`：智能体 act 步骤的请求，原生 `tools`、流式；报告服务返回的是原生 `tool_calls`，还是只能靠文本解析兜底；
- `text`：`awm agent` 的第一个请求，不带 `tools`；检查 `<tool_call>` 文本是否留在 `content` 里。

机器要求、安装、启动与排错见 [docs/DEPLOYMENT.md](DEPLOYMENT.md) 的「模型服务」与「常见问题」两节。

接 OpenAI 兼容端点时只设环境变量：`WORKBENCH_LLM__BACKEND=openai_compat`，`WORKBENCH_LLM__BASE_URL` 与 `WORKBENCH_LLM__MODEL` 指定端点和模型，`WORKBENCH_LLM__API_KEY_ENV` 写存放 key 的环境变量名，例如 `DEEPSEEK_API_KEY`，默认是 `OPENAI_API_KEY`。key 只放在进程环境里，不写入任何文件。`workbench doctor` 的 `env-vars` 一行检查这个变量是否存在，`llm` 一行请求 `<base_url>/models`。[docs/examples/e-commerce-33-deepseek.md](examples/e-commerce-33-deepseek.md) 记录了用 DeepSeek 在官方 `e_commerce_33` 上的一次真实运行，包括工具调用序列、审批、DB diff，以及 `awm agent` 与 `workbench verify` 的链路；它是一次演示，不是评测。

阅读：

- `configs/serving/arctic-awm-4b.yaml`：每个参数都注明了 vLLM v0.19.0 的源码位置；
- `src/workbench/llm/client.py`：httpx 分阶段超时加 `asyncio.timeout` 墙钟，只对网络错误和 5xx 重试；
- `src/workbench/llm/backends/openai_compat.py`（流式 SSE）、`src/workbench/llm/backends/mock_replay.py`（回放手写脚本）；
- `src/workbench/llm/toolcall_parse.py`：`<tool_call>` 文本解析；
- `src/workbench/llm/reasoning.py`：DeepSeek 思考模式要求带 `tools` 的请求回传此前各轮的 `reasoning_content`，由 LLM 层补上（ADR-015）；
- `src/workbench/llm/probe.py`、`src/workbench/llm/serving.py`；
- ADR-016：act 只经原生 `tools` 传工具定义，以及 `llm.max_tokens` 与 `agent.token_budget` 的默认值；
- 测试：`tests/unit/test_llm_client.py`、`tests/unit/test_mock_replay.py`、`tests/unit/test_llm_reasoning.py`、`tests/unit/test_llm_probe.py`。

## 4. 网关：路由、策略、审计

目标：理解为什么所有工具调用都必须经过网关（ADR-005、ADR-006、ADR-007、ADR-009）。

```bash
workbench gateway export-risk --dataset-dir tests/fixtures/awm_mini --out /tmp/risk_table.csv
```

输出：

```
wrote 7 rows to /tmp/risk_table.csv (offline: names and route methods; live sessions add descriptions)
POST/PUT/PATCH/DELETE tools graded read: 0 by name alone, 0 with the HTTP-method floor
```

CSV 节选：

```
scenario,tool,http_method,risk,source,reason,requires_approval,heuristic_risk
mini_e_commerce,search_products,GET,read,heuristic,verb 'search' => read,False,read
mini_e_commerce,add_item_to_cart,POST,write,heuristic,verb 'add' => write,True,write
mini_e_commerce,remove_cart_item,DELETE,destructive,heuristic,verb 'remove' => destructive,True,destructive
```

风险级别先按工具名中的动词判断，未知动词按 `write` 处理；再以路由的 HTTP 方法为下限：DELETE 至少为 `destructive`，POST/PUT/PATCH 至少为 `write`（ADR-006）。`heuristic_risk` 列是只看名称的结果，用来对照下限改变了什么。迷你夹具的方法与名称一致，所以两个计数都是 0。在官方数据集上，加上下限后没有任何 POST/PUT/PATCH/DELETE 路由被判为 `read`，由 `tests/integration/test_official_data.py` 的 `test_http_method_floor_on_official_routes` 检查。

每次调用在网关中依次经过：按会话路由，工具名为 `<scenario>__<tool>`，不同场景不会冲突；deny-first 策略与审批；限流；调用 AWM server；把结果归一为 ok、empty 或 error；写一条审计。区分空结果与错误的规则依据 AWM 的实际返回形态（ADR-007，docs/UPSTREAM.md §6.3）。

`workbench gateway serve` 把网关作为独立的 MCP server 运行，MCP 在 `/mcp`，管理接口在 `/admin`，任何 MCP 客户端都能连接；API 进程内的网关挂在 `/gateway/mcp`。客户端用 `X-Workbench-Session` 头指明会话，用 `X-Approval-Token` 头携带审批令牌。

阅读：

- `configs/tool_policy.yaml`：动词表、未知动词的默认级别、需要审批的级别、限流参数；
- `src/workbench/envs/catalog.py` 的 `route_methods`：用 `ast` 从场景代码中读出每个工具的 HTTP 方法，不执行代码；
- `src/workbench/gateway/policy.py`：`classify`（deny-first 分级）、`PolicyEngine`，以及 `ApprovalService`（HMAC 一次性审批令牌，绑定会话、工具、参数摘要与预演 digest）；
- `src/workbench/gateway/core.py`：路由、调用流程与审计；`src/workbench/gateway/ratelimit.py`；
- `src/workbench/gateway/audit.py`：PII 脱敏，时间戳不会被当成电话号码（ADR-014）；
- `src/workbench/gateway/errors.py`：ok / empty / error 归一；
- `src/workbench/gateway/server.py`：低层 MCP Server 与 `/admin` 接口；
- 测试：`tests/unit/test_gateway_core.py`、`tests/unit/test_gateway_policy.py`、`tests/unit/test_gateway_misc.py`、`tests/unit/test_catalog.py`、`tests/integration/test_gateway_real_awm.py`、`tests/integration/test_official_data.py`。

### 4.1 审批前预演（ADR-026）

审批策略交给人工的 write 与 destructive 调用，在审批之前先在影子环境里执行一次。env-manager 用 SQLite 在线备份复制会话当前的数据库，在租用的端口上以独立进程组启动一个 AWM server，执行同一调用，算出行级改动，然后回收进程组、端口与目录。会话自己的数据库只被读取。同时运行的影子环境不超过 `env.max_previews`（默认 2），不占 `env.max_envs` 的名额。审批卡片显示"将要改动的行"；预演失败时显示原因。

配置在 `configs/app.yaml` 的 `approval` 一节：

- `require_preview`：按风险级别设置，默认 `{write: false, destructive: true}`。为 true 而预演失败时不签发令牌，只能拒绝：UI 禁用 Approve，`POST /approvals/{sid}` 返回 409。为 false 时仍可批准，令牌绑定 `preview_unavailable`，审计中记录这一点，UI 标出"未预演"。
- `preview_timeout_s`：整个预演的超时，默认 30 秒。
- `preview_max_rows`：卡片上每张表显示的行数，默认 20；比对时使用全部主键。

复现预演失败的界面：

```bash
WORKBENCH_APPROVAL__PREVIEW_TIMEOUT_S=0.2 WORKBENCH_APPROVAL__REQUIRE_PREVIEW='{"write": true, "destructive": true}' make demo-mock
```

影子 server 在 0.2 秒内起不来，预演以超时失败；write 也要求预演，所以加购的审批卡片只能拒绝。

预演各阶段的耗时是工程数字，与机器有关，用脚本测量：

```bash
uv run python scripts/measure_preview.py                              # 迷你夹具，10 次
uv run python scripts/measure_preview.py --dataset-dir data/awm1k --scenario e_commerce_33 \
  --args '{"product_offer_id": 1, "quantity": 1}'                     # 官方场景，需要 make data
```

脚本启动一个会话环境，对同一调用预演 `--runs` 次（默认 10），打印机器信息与 prepare、start、call、diff、reclaim、total 各阶段的最小值、中位数和最大值（毫秒），并按"最慢总耗时的 5 倍，向上取整到 5 秒"建议 `preview_timeout_s`。默认值 30 就是按这条规则在官方 `e_commerce_33` 上得出的。脚本不调用 LLM。

批准后，网关在真实调用前后各取一次数据库状态，把实际改动与令牌绑定的预演做结构比对：比对改动的表、新增、删除与修改的主键，以及改动的列名。时间列只记录、不比对，包括声明类型含 DATE 或 TIME 的列、默认值为当前时间的列和名称像时间的列。单列主键不是 INTEGER rowid 别名的表，主键可能由 server 生成，新增行只比较行数。结果写入审计的 `preview_check`，`result` 取 `match`、`preview_mismatch`、`preview_unavailable` 或 `check_failed`；不一致时 UI 标出 `preview_mismatch`。这些规则的依据见 docs/UPSTREAM.md §10.3。

外部 MCP 客户端的流程：先 `POST /admin/previews` 取得预演记录，再把记录的 `id` 作为 `preview_id` 交给 `POST /admin/approvals`，请求体还包括 `session_id`、`tool`、`arguments` 与 `approver`；也可以不给 `preview_id`，由后者先预演再签发。返回的令牌放进 `X-Approval-Token` 头调用工具。审批策略拒绝的调用和必需预演失败的调用，`/admin/approvals` 都返回 409。`workbench gateway serve` 只有在配置了 `env.manager_url` 时才能预演；没有 env-manager 时预演不可用，要求预演的级别（默认 destructive）在这里无法批准。

阅读：

- `src/workbench/envs/manager.py` 的 `preview`、`checkpoint`、`changes_since`；
- `src/workbench/envs/changes.py`：行级改动与结构比对；
- `src/workbench/gateway/core.py` 的 `preview`、`issue_approval`、`_check_preview`；
- `src/workbench/agent/nodes/preview.py`；
- 测试：`tests/unit/test_changes.py`、`tests/unit/test_gateway_preview.py`、`tests/integration/test_preview_real_awm.py`（真实 AWM server：预演与真实 diff 一致、destructive 调用、失败路径、回收后无残留）。

### 4.2 审批策略（ADR-027）

策略文件由 `approval.policy_file` 指定，默认 `configs/approval_policy.yaml`。规则按顺序匹配，第一条命中的规则决定这次调用是 `auto_approve`、`require_human` 还是 `deny`。没有命中时，write 与 destructive 需要人工审批，read 不需要。destructive 调用永远不会被自动批准：加载时拒绝把 `destructive` 与 `auto_approve` 写进同一条规则，匹配时跳过对 destructive 调用的 `auto_approve` 规则，网关以策略名义签发令牌前再查一次。

规则可以按工具名与场景名（glob）、风险级别和参数条件匹配。参数条件有数值比较 `lt`、`lte`、`gt`、`gte`、`eq`、`ne` 和枚举成员 `in`、`not_in`。条件从严：缺少参数时不命中；无法比较的值让 `deny` 与 `require_human` 规则命中，让 `auto_approve` 规则不命中。字段说明写在默认策略文件开头的注释里。文件在网关启动时按 schema 校验，出现未知字段或类型错误时报出每个问题的 YAML 路径并停止。

两份策略文件：

- 默认策略 `configs/approval_policy.yaml` 不自动批准任何调用。它只有两条规则：`no-payment-method-deletion` 禁止删除已保存的支付方式（deny）；`bulk-cart-add-needs-human` 把单次加购超过 5 件交给人工（require_human）。自动批准的规则只作为注释掉的示例留在文件里，需由管理员显式开启。原因是自动批准的调用既不预演，也没有人确认，而策略只看调用本身（README 的「设计边界」）；官方环境又会接受一些错误写入，例如向购物车加入不存在的 offer 也会成功（docs/UPSTREAM.md §6.3）。
- 演示策略 `configs/approval_policy.demo.yaml` 三种决策各一例：上面两条，加上 `small-cart-add-official`，即官方 e-commerce 场景中单次加购不超过 2 件自动批准。`make demo-mock`、docker 冒烟测试 `scripts/docker_smoke.py` 和相关测试使用它。迷你演示的加购不命中其中任何规则，仍然走预演与审批卡片。

`curl -s http://127.0.0.1:8080/healthz` 的 `approval_policy` 字段显示当前生效的策略文件，以及每条规则的 `<id> (<decision>)`。

离线试一次调用，不启动任何东西。不带 `--policy` 时使用 `approval.policy_file`，即默认策略：

```bash
workbench gateway policy test --dataset-dir tests/fixtures/awm_mini \
  --scenario mini_e_commerce --tool add_item_to_cart --args '{"product_offer_id": 11, "quantity": 1}'
# decision: require_human (default) - no rule matched: write calls need a human by default
workbench gateway policy test --dataset-dir tests/fixtures/awm_mini \
  --tool mini_e_commerce__delete_user_payment_method --args '{"payment_method_id": 2}'
# decision: deny (rule no-payment-method-deletion) - rule no-payment-method-deletion: Saved payment methods are never deleted by the agent.
```

官方场景中加购 2 件，演示策略自动批准，默认策略交给人工。`--risk` 直接给出风险级别，所以不需要官方数据集：

```bash
workbench gateway policy test --policy configs/approval_policy.demo.yaml \
  --tool e_commerce_33__add_item_to_cart --risk write --args '{"product_offer_id": 1, "quantity": 2}'
# decision: auto_approve (rule small-cart-add-official) - rule small-cart-add-official: On the official e-commerce scenarios, adding up to 2 units is approved automatically.
workbench gateway policy test \
  --tool e_commerce_33__add_item_to_cart --risk write --args '{"product_offer_id": 1, "quantity": 2}'
# decision: require_human (default) - no rule matched: write calls need a human by default
```

输出在 decision 之前还列出风险级别的来源，以及每条规则是否命中和原因；destructive 保护跳过了某条规则时，另起一行说明。

三种决策在运行时的表现：

- `require_human`：先预演，再由人在审批卡片上批准。卡片上"审批策略"一行写明命中的规则，没有命中时显示 default。
- `auto_approve`：不预演，也不弹出审批卡片。网关自己签发令牌，批准人为 `policy:<规则 id>`，令牌绑定 `preview_unavailable`。执行后照常测量实际改动，与规则 id 一起写入审计；时间线显示 "auto-approved by the approval policy" 和实际改动。
- `deny`：网关以 `policy_denied_by_rule` 拒绝，带着令牌也拒绝，也不能再申请审批。

每条审计的 `policy` 字段记录 `decision`、`rule`（没有命中规则时为 null）、`reason` 与 `guard`。

演示策略的自动批准规则只针对官方场景，迷你演示不会命中。要在演示中看自动批准，可以用一个临时策略文件，不改仓库里的文件；环境变量 `WORKBENCH_APPROVAL__POLICY_FILE` 优先于演示策略：

```bash
cat > /tmp/demo-policy.yaml <<'YAML'
version: 1
rules:
  - id: demo-small-add
    tools: [add_item_to_cart]
    risk: [write]
    args: {quantity: {lte: 2}}
    decision: auto_approve
YAML
workbench gateway policy test --policy /tmp/demo-policy.yaml --dataset-dir tests/fixtures/awm_mini \
  --scenario mini_e_commerce --tool add_item_to_cart --args '{"product_offer_id": 11, "quantity": 1}'
# decision: auto_approve (rule demo-small-add) - rule demo-small-add
WORKBENCH_APPROVAL__POLICY_FILE=/tmp/demo-policy.yaml make demo-mock
tail -n 1 data/demo/audit.jsonl | python3 -m json.tool   # 看 policy.rule、approver 与 preview_check.actual
```

阅读：

- `src/workbench/gateway/approval_policy.py`：schema、匹配、从严的参数条件、destructive 保护；
- `src/workbench/gateway/core.py` 的 `approval_verdict` 与 `_call`；
- `src/workbench/agent/nodes/act.py`：按决策分流，只有 `require_human` 进入预演与审批；
- 依赖库的行为依据：docs/UPSTREAM.md §10.4；
- 测试：`tests/unit/test_approval_policy.py`（其中 `test_the_default_policy_auto_approves_nothing` 检查默认策略没有 auto_approve 规则、取消注释示例后恰好等于演示策略，`test_the_demo_policy_shows_each_decision_and_keeps_the_demo_call_for_a_person` 检查演示策略）、`tests/unit/test_gateway_approval_policy.py`、`tests/unit/test_agent_approval_policy.py`，以及 `tests/unit/test_cli.py` 中以 `test_gateway_policy_test_` 开头的测试和 `test_demo_mode_uses_the_demo_approval_policy`。

## 5. 智能体与 UI：LangGraph 状态机

目标：跑通"查询 → 预演与审批 → 写入 → 回答 → DB diff"的完整链路。

命令行，mock LLM 按手写脚本回放：

```bash
WORKBENCH_LLM__MOCK_FIXTURE=tests/fixtures/trajectories/demo_query_write_approve.jsonl \
workbench agent run --scenario mini_e_commerce --dataset-dir tests/fixtures/awm_mini --approve auto \
  "Add the best wireless noise cancelling headphones under \$200 to my cart"
```

`agent run` 在本进程内启动一个新的隔离会话，不需要 `env serve`；配置了 `env.manager_url` 时改用远程 env-manager。它先打印会话 id、工具数与 MCP 地址，然后每个事件一行：工具调用及其状态与网关决定、`approval_required` / `approval_granted`、`memory_saved`、最终回答，最后以 JSON 打印 DB diff。脚本的内容是：计划两步，调用 `search_products`，再调用 `add_item_to_cart`（`product_offer_id` 11，数量 1）。默认策略对这次加购给出 `require_human`，于是先预演，再由 `--approve auto` 代替人批准；批准人默认为 `cli-user`，可以用 `--approver alice` 指定。之后给出回答；verify 节点写入来源为 `user_stated` 的记忆 `headphone_budget`，拒绝来源为 `inferred` 的那条。DB diff 显示 `cart_items` 新增一行。`--approve prompt`（默认）在终端询问，`--approve deny` 拒绝，智能体随后重新规划。

浏览器：

```bash
make demo-mock     # 打开 http://127.0.0.1:8080/ui/
```

演示模式使用迷你场景、同一份 mock 脚本和演示策略，会话数据、审计、checkpoint 与记忆都写在 `data/demo/` 下。在 UI 中：选择场景 `mini_e_commerce` → Start isolated session → 发送上面的请求 → 在审批卡片上查看"将要改动的行"并点 Approve → 查看 Timeline 与 DB diff 标签页。Trajectory viewer 标签页可以加载 `awm agent` 输出目录中的 `trajectory.json`。

mock 回答来自手写脚本，不是模型输出。

`scripts/demo_ui_check.py` 用 Playwright 自动走一遍同样的流程，把整页截图存到指定目录，并把 README 用的三张截图写到 `docs/assets/`：

```bash
uv run --with playwright python scripts/demo_ui_check.py <截图目录> [chromium 路径]   # 先在另一个终端运行 make demo-mock
```

阅读：

- `src/workbench/agent/graph.py`、`src/workbench/agent/nodes/*.py`：尤其 `approve.py` 中的 `interrupt` 与恢复。被中断的节点恢复时会重新执行，所以令牌在恢复后才签发，预演也放在单独的 `preview` 节点，只执行一次（docs/UPSTREAM.md §10.1）；
- `src/workbench/agent/guards.py`：步数、重复调用、连续无变化、token 预算、墙钟；
- `src/workbench/agent/memory.py`：长期记忆只接受 `user_stated` 与 `tool_result` 两种来源（ADR-010）；
- `src/workbench/agent/prompts/*.md` 与 `src/workbench/agent/prompts.py`：版本化的 prompt 文件，版本记录见 [docs/ARCHITECTURE.md](ARCHITECTURE.md#4-prompt-版本) §4；
- `src/workbench/runtime.py`、`src/workbench/api/app.py`、`src/workbench/api/sse.py`：会话、SSE、审批接口、会话上限 `api.max_sessions`，同一会话同时只跑一轮；
- `src/workbench/obs/tracing.py`、`src/workbench/obs/metrics.py`：trace JSONL 与 Prometheus 指标 `/metrics`；
- `ui/app.js`：审批卡片、时间线、DB diff、轨迹查看器；
- `src/workbench/verify.py`：`workbench verify` 运行一次 `awm verify`，code 模式不调用 LLM，sql 模式经本地代理调用裁判，校验代码拿不到 key（ADR-023，docs/UPSTREAM.md §6.5）；
- 测试：`tests/unit/test_agent_e2e.py`（正常完成、审批、拒绝后重新规划、各类守卫、重启后从 checkpoint 恢复、记忆门槛）、`tests/unit/test_agent_parts.py`、`tests/unit/test_api.py`、`tests/integration/test_agent_real_awm.py`、`tests/integration/test_verify_real_awm.py`。

## 6. 训练启动器：smoke profile

目标：理解训练如何与 app 隔离（ADR-002），以及为什么只有 smoke profile（ADR-012）。

```bash
workbench train preflight     # 检查 GPU、CUDA/torch/vLLM/veRL、配置键与数据
workbench train launch        # 默认 dry-run，只打印计划；--execute 才启动训练
```

在没有 GPU、没有安装 train 环境的机器上，preflight 中只有 `profile` 与 `data` 两项为 ok：

```
│ profile       │ ok     │ smoke: Qwen/Qwen3-0.6B (0.6B), LoRA, 3 steps, NO_RESULTS
│ data          │ ok     │ 8 rows in configs/train/smoke_data.json
```

`gpu`、`train-env`、`verl`、`config-keys`、`hydra-compose` 为 fail，退出码为 1。launch 的 dry-run 照样以退出码 0 打印计划：训练命令 `uv run --project train --no-sync python -m agentfly.cli train` 加上 `configs/train/smoke.yaml` 展开的 Hydra 覆盖项；训练进程会拿到与拿不到的环境变量名，只列名称、不列值（ADR-024）；以及 preflight 结果。`--execute` 要求 preflight 全部通过，运行目录在 `data/train_runs/` 下，并总是带 `NO_RESULTS` 标记。

在 GPU 机器上：

1. `git -C third_party/AgentFly submodule update --init verl`：这个嵌套子模块的 URL 是 SSH 形式，需要 SSH key 或 `insteadOf` 改写；
2. `cd train && uv sync`；
3. `workbench train preflight` 全部为 ok 后，执行 `workbench train launch --execute`。

机器要求、flash-attn 的构建（ADR-025）与排错见 [docs/DEPLOYMENT.md](DEPLOYMENT.md) 的「smoke 训练」与「常见问题」两节。把 GPU 机器上的日志交给别人之前，用 `uv run python scripts/redact_log.py <日志文件>` 写出脱敏副本，其中的 key、token、邮箱、卡号与电话被替换成标记；规则的范围与分享前的检查见同一文档的「分享日志前脱敏」一节。

阅读：

- `configs/train/smoke.yaml` 与 `configs/train/README.md`：每个 Hydra 键都来自固定 veRL fork 的实际配置（docs/UPSTREAM.md §7.2）；
- `src/workbench/train/profile.py`：profile 约束，模型不超过 1.7B、必须 LoRA、不超过 5 step；
- `src/workbench/train/preflight.py`、`src/workbench/train/launch.py`；
- `src/workbench/subprocess_env.py` 的 `train_env`：训练进程只拿白名单中的环境变量，机器确实需要的额外变量名写进 `train.env_passthrough`；
- `train/pyproject.toml`：独立的 train 环境，拆成两个环境的原因见 docs/UPSTREAM.md §8；
- 测试：`tests/unit/test_train.py`（GPU 信息为 mock）、`tests/unit/test_train_env.py`。

## 7. 数字纪律

目标：理解仓库怎样保证文档中不出现未登记的性能数字（ADR-013）。

```bash
make check-numbers    # workbench results check
make results          # workbench results render：由 results/registry.yaml 重新生成 docs/RESULTS.md
```

- 应用层功能不做效果评测。性能数字只有两个来源：论文报告值与批量实验的报告。
- 论文报告值登记在 `results/registry.yaml`，即 arXiv 2602.10090 v3 的数值，每条带 `source` 与 `verified`。引用这些数值的页面必须带上免责声明，原文是 `src/workbench/results/registry.py` 中的 `DISCLAIMER`。
- 批量实验的数字来自提交在 `results/lab/REPORT.md` 的一次完整运行的报告（ADR-028），引用它的页面必须链接到这份报告。
- `workbench results check` 扫描 README 与 `docs/**/*.md` 中像性能数字的写法：百分数、两位小数、Pass@k。每一处都必须是 registry 中的值、实验报告中的值，或在 `configs/number_whitelist.yaml` 中带理由列出；应用层效果类措辞直接报错。有问题时退出码为 1。
- 改动 README 或 docs 后运行 `make check-numbers`，改动 registry 后运行 `make results`。`make check-links` 检查 README 与 docs 中所有相对链接和锚点。

阅读：`results/registry.yaml`、`src/workbench/results/registry.py`、`src/workbench/results/check_numbers.py`、`configs/number_whitelist.yaml`、`docs/RESULTS.md`、`scripts/check_links.py`；测试 `tests/unit/test_results.py`、`tests/unit/test_check_links.py`。

## 8. 批量实验：在官方任务上评测与训练

目标：理解 `workbench lab` 怎样把整条链路放到大量官方任务上运行、判定、训练和统计（ADR-028）。设计见 [EXPERIMENTS.md](EXPERIMENTS.md)，一次完整运行的结果见 [LAB_RESULTS.md](LAB_RESULTS.md)。

不需要 GPU 也能看懂每一步：划分与 verifier 下限只用 CPU。下载官方数据集后：

```bash
workbench lab split --test-tasks 12 --val-tasks 6 --train-tasks 16 --inject-tasks 8 --lab-dir data/lab-cpu
workbench lab eval --split test --tag null-test --backend null --lab-dir data/lab-cpu   # 什么都不做的智能体
workbench lab report --lab-dir data/lab-cpu
```

- `split` 按场景划分，写出 `splits.json`；`eval --backend null` 跑出被 verifier 误判为成功的"平凡任务"；`report` 从运行记录生成 `REPORT.md` 与 `summary.json`。
- 换成 `--backend vllm` 或 `--backend openai_compat` 就是真实模型的评测；`sft-data`、`select`、`bench` 与 `scripts/lab/sft_train.py` 组成训练与选择；`scripts/lab/run_all.sh` 按顺序跑完全部阶段，可断点续跑。

阅读：

- `src/workbench/lab/splits.py`：场景级划分、含 destructive 工具的注入场景；
- `src/workbench/lab/episodes.py`：并发运行、approver、`NullBackend`、`InjectingGateway`、基础设施错误的重试；
- `src/workbench/lab/recorder.py`、`src/workbench/lab/sft_data.py`：LLM 调用录制与 SFT 样本；
- `src/workbench/lab/metrics.py`、`src/workbench/lab/report.py`：失败分类、McNemar、配对 bootstrap、Wilson 区间、pass@k；
- `src/workbench/lab/serving_bench.py`：回放录制的请求压测推理服务；
- `scripts/lab/sft_train.py`、`scripts/lab/risk_model.py`、`scripts/lab/run_all.sh`；
- 测试：`tests/unit/test_lab.py`。
