# WALKTHROUGH — 学习路线

面向仓库主人。按"环境 → 合成 → 服务 → 网关 → 智能体 → 训练循环"的顺序，每一步给出命令、预期输出和建议阅读的源文件。所有命令在仓库根目录执行，除第 3、6 步中标注 UNVERIFIED-LOCAL 的部分外，都能在 CPU + mock LLM 下运行。下文的"预期输出"摘自开发沙箱中的实际运行（2026-09-24），会话 ID、端口等随每次运行变化。第 3.1、5.1、5.2 小节是用 DeepSeek 做的**单次链路演示，不构成评测**：需要 `DEEPSEEK_API_KEY`（外部 API，会产生费用），输出是 2026-09-24 那一次运行的原文节选。

准备：

```bash
make setup      # 初始化两个 submodule、安装 app 环境、生成迷你夹具、校验 train 锁文件
make doctor     # 缺少官方数据集和 GPU 只会给出 warn，不算失败
```

预期 `make doctor` 输出（节选）：

```
│ python                      │ ok     │ 3.12 (AWM and AgentFly require 3.12)  │
│ submodule:agent-world-model │ ok     │ pinned at 85e322f69279                │
│ submodule:AgentFly          │ ok     │ pinned at 1256586b1109                │
│ dataset                     │ warn   │ data/awm1k: missing gen_tasks.jsonl, … (run `make data`)
│ gpu                         │ warn   │ no nvidia-smi: GPU steps are UNVERIFIED-LOCAL (mock mode still works)
│ llm                         │ ok     │ mock_replay fixture …                 │
```

下面默认使用手写的迷你场景 `tests/fixtures/awm_mini`（按 AWM 数据格式编写的 7 个工具的迷你电商，不是官方数据；工具名与参数已于 2026-09-24 与官方 `e_commerce_33` 对账，见 `docs/verification/2026-09-24-fixture-reconciliation.md`）。

如果已用 `make data` 下载官方数据集，可以把命令中的 `mini_e_commerce` 换成官方场景（例如 `e_commerce_33`），并去掉 `--dataset-dir` 参数：`workbench env up e_commerce_33` 启动后 `list_tools` 返回 39 个工具（`docs/verification/2026-09-24-dataset.md`）。

先读：`TASK.md`（任务书）、`CLAUDE.md`（规则摘要）、`docs/RECON.md`（上游事实与行号）、`docs/ARCHITECTURE.md`。

---

## 1. 环境：隔离的 AWM 环境会话

目标：理解"一个会话 = 一份独立的 SQLite + 一个 AWM MCP server 子进程"。

```bash
export WORKBENCH_ENV__DATASET_DIR=tests/fixtures/awm_mini
workbench env search cart --dataset-dir tests/fixtures/awm_mini   # 场景目录检索
workbench env serve &                                              # env-manager 控制面，默认 127.0.0.1:8090
workbench env up mini_e_commerce --session-id wt1                  # 启动隔离会话
workbench env ls
workbench env snapshot wt1 before
workbench env diff wt1                                             # 与 initial.db 比较
workbench env down wt1
```

（命令前的 `uv run` 省略，下同。）

预期输出（节选）：

```
│ scenario        │ tools │ tasks │ tables │
│ mini_e_commerce │ 7     │ 2     │ 6      │

│ session │ scenario        │ state   │ url                    │ tools │
│ wt1     │ mini_e_commerce │ healthy │ http://127.0.0.1:1810… │ 7     │

snapshot before saved
{ "changed": false, "tables_added": [], "tables_removed": [], "tables": {} }
stopped wt1
```

阅读：

- `src/workbench/envs/awm_adapter.py`：如何调用 AWM 建库与启动 server，为什么必须显式传 `--db_path`、`--temp_server_path`、`--output_dir`（`docs/RECON.md` §1）；
- `src/workbench/envs/manager.py`：进程组启动与 `killpg`、健康检查截止时间、排队信号量、空闲回收；server 执行生成代码，只拿到白名单中的环境变量（`src/workbench/subprocess_env.py`，ADR-019）；
- `src/workbench/envs/snapshot.py`：快照、恢复、按主键的表级 diff；
- `src/workbench/envs/service.py`、`envs/http.py`：本地与远程两种 env 服务；
- 测试：`tests/unit/test_env_manager.py`、`tests/unit/test_subprocess_env.py`、`tests/integration/test_env_real_awm.py`（真实启动 AWM server，并检查进程组中没有任何 key）。

## 2. 合成：编排 AWM 的生成流水线

目标：理解如何在不改官方数据的前提下，把 `awm gen` 各子步骤组织成可续跑、可记账、可校验的流水线（ADR-011）。

```bash
workbench synth run --scenarios 2 --out data/synth/demo           # 默认 dry-run，只打印计划
# 真正执行需要 LLM key，并显式加 --execute（UNVERIFIED-LOCAL，未在本仓库执行过）
workbench synth validate --help
```

预期 dry-run 输出（节选）：

```
{
  "run_dir": "data/synth/demo",
  "mode": "dry-run",
  "origin": "local-synth",
  "seed": "third_party/agent-world-model/outputs/seed_scenario.jsonl (copied into run dir)",
  "required_env": ["OPENAI_API_KEY", "AWM_SYN_OVERRIDE_MODEL", "EMBEDDING_OPENAI_API_KEY"],
  ...
  "post": ["awm env reset_db (run databases)", "awm env check_all -> validation report"]
}
```

dry-run 不创建任何目录。

阅读：

- `src/workbench/synth/runner.py`：步骤计划、`state.json` checkpoint、manifest（`origin: local-synth`）、种子文件复制；各步骤的环境变量（gen 步骤只拿到占位 key，reset_db 与 check_all 不拿任何 key，ADR-019）；
- `src/workbench/synth/proxy.py`、`synth/ledger.py`：本地 LLM 代理（缓存、重试、按步骤记账），真实 key 只在代理中；
- `src/workbench/synth/validate.py`：`awm env check_all` 结果的分类报告；
- `configs/pricing.yaml`（占位价格）；
- 测试：`tests/unit/test_synth.py`、`tests/integration/test_synth_validate_real_awm.py`。

## 3. 服务：模型服务与 LLM 客户端

目标：理解 LLM 客户端的后端抽象、两层超时（ADR-008），以及为什么从正文中解析 `<tool_call>`。

```bash
workbench serve vllm-cmd                  # 只打印命令
```

预期输出：

```
vllm serve Snowflake/Arctic-AWM-4B --host 127.0.0.1 --port 8000 --served-model-name Snowflake/Arctic-AWM-4B --gpu-memory-utilization 0.9 --enable-auto-tool-choice --tool-call-parser hermes
```

最后两个参数自 2026-09-24 起由 profile 启用（仓库主人的决定 D11，ADR-020）：智能体的 act 请求带原生 `tools`，vLLM 需要 tool parser 才接受；不带 `tools` 的请求（如 `awm agent`）不经过 parser。依据是读源码，尚未在 GPU 上验证。

在 GPU 机器上运行 `scripts/serve_vllm.sh`，然后设置 `WORKBENCH_LLM__BACKEND=vllm`（UNVERIFIED-LOCAL：需要 CUDA GPU 和 `huggingface.co` 访问）。

### 3.1 OpenAI 兼容端点：DeepSeek（单次链路演示，不构成评测）

后端只用环境变量配置，key 只从 `DEEPSEEK_API_KEY` 读取，不写入任何文件：

```bash
export WORKBENCH_LLM__BACKEND=openai_compat WORKBENCH_LLM__BASE_URL=https://api.deepseek.com \
       WORKBENCH_LLM__MODEL=deepseek-flash WORKBENCH_LLM__API_KEY_ENV=DEEPSEEK_API_KEY
workbench doctor
```

2026-09-24 的实际输出（节选）：

```
│ env-vars                    │ ok     │ backend=openai_compat                                                 │
│ llm                         │ ok     │ https://api.deepseek.com/models reachable                             │
```

要点（详见 `docs/verification/2026-09-24-llm-chain.md` §1–2）：

- DeepSeek 在流式响应中返回原生 `tool_calls`，由 `openai_compat.py` 直接解析；不需要走 `<tool_call>` 文本解析。
- DeepSeek 默认开启思考模式：思考 token 计入输出 token；`temperature` 在思考模式下不生效。
- DeepSeek 文档要求带 `tools` 的请求回传此前各轮的 `reasoning_content`。Phase 12.5 起由 LLM 层按文档回传（`src/workbench/llm/reasoning.py`，ADR-017）；文档没写清的情况见 LIMITATIONS §6。

阅读：

- `configs/serving/arctic-awm-4b.yaml`：每个参数都注明了 vLLM v0.19.0 的源码位置；
- `src/workbench/llm/client.py`：httpx 分阶段超时 + `asyncio.timeout` 墙钟，只对网络错误和 5xx 重试；
- `src/workbench/llm/backends/openai_compat.py`（流式 SSE）、`backends/mock_replay.py`（手写脚本回放）；
- `src/workbench/llm/toolcall_parse.py`、`llm/reasoning.py`（按 DeepSeek 文档回传 `reasoning_content`，ADR-017）；
- 测试：`tests/unit/test_llm_client.py`、`tests/unit/test_mock_replay.py`。

## 4. 网关：策略、审批、审计

目标：理解为什么所有工具调用都必须经过网关（ADR-005/006/007/009/015）。

```bash
workbench gateway export-risk --dataset-dir tests/fixtures/awm_mini --out data/risk_table.csv
```

预期输出：

```
wrote 7 rows to data/risk_table.csv (offline: names and route methods; live sessions add descriptions)
POST/PUT/PATCH/DELETE tools graded read: 0 by name alone, 0 with the HTTP-method floor

scenario,tool,http_method,risk,source,reason,requires_approval,heuristic_risk
mini_e_commerce,search_products,GET,read,heuristic,verb 'search' => read,False,read
mini_e_commerce,add_item_to_cart,POST,write,heuristic,verb 'add' => write,True,write
mini_e_commerce,remove_cart_item,DELETE,destructive,heuristic,verb 'remove' => destructive,True,destructive
...
```

风险级别先按工具名动词判断，再以路由的 HTTP 方法为下限（DELETE 至少 `destructive`，POST/PUT/PATCH 至少 `write`，ADR-015）。迷你夹具的方法与名称一致，所以没有变化；在官方数据集上，POST/PUT/PATCH/DELETE 工具中被判为 `read` 的从 38 个降到 0 个（`docs/verification/logs/2026-09-24-phase12.5-risk-floor.log`）。

`workbench gateway serve` 可以把网关作为独立的 MCP server 运行，任何 MCP 客户端都能连接。

阅读：

- `configs/tool_policy.yaml`：动词表、未知动词默认按 `write` 处理、需要审批的风险级别、限流参数；
- `src/workbench/envs/catalog.py` 的 `route_methods`：用 `ast` 从场景代码中读出每个工具的 HTTP 方法（不执行代码）；
- `src/workbench/gateway/policy.py`（deny-first 分级）、`gateway/core.py`（HMAC 一次性审批令牌，绑定会话、工具和参数摘要）、`gateway/ratelimit.py`、`gateway/audit.py`（PII 脱敏）、`gateway/errors.py`（ok / empty / error 归一，依据 AWM 的实际错误文本，见 RECON §10）；
- `src/workbench/gateway/server.py`：低层 MCP Server，工具名为 `<scenario>__<tool>`；
- 测试：`tests/unit/test_gateway_*.py`、`tests/integration/test_gateway_real_awm.py`。

## 5. 智能体：LangGraph 状态机与 UI

目标：跑通"查询 → 审批 → 写入 → 回答 → DB diff"的完整链路。

命令行（自动批准，mock LLM 按手写脚本回放）：

```bash
WORKBENCH_LLM__MOCK_FIXTURE=tests/fixtures/trajectories/demo_query_write_approve.jsonl \
workbench agent run --scenario mini_e_commerce --dataset-dir tests/fixtures/awm_mini --approve auto \
  "Add the best wireless noise cancelling headphones under \$200 to my cart"
```

预期输出（节选）：

```
session 2fa16aff8b2e: 7 tools from http://127.0.0.1:18100/mcp
tool mini_e_commerce__search_products -> ok (allowed)
approval_required mini_e_commerce__add_item_to_cart
approval_granted mini_e_commerce__add_item_to_cart
tool mini_e_commerce__add_item_to_cart -> ok (allowed)
memory_saved headphone_budget
answer I added 'Wireless Noise Cancelling Headphones A' ($189) to your cart (quantity 1).
{ "changed": true, ... "cart_items": { "rows_before": 1, "rows_after": 2, "added": [2], ... } }
```

浏览器：

```bash
make demo-mock          # 打开 http://127.0.0.1:8080/ui/
```

在 UI 中：选择场景 → Start isolated session → 发送上面的请求 → 在审批面板点 Approve → 查看 Timeline 与 DB diff 标签页。`scripts/demo_ui_check.py` 用 Playwright 自动走一遍同样的流程。

注意：mock 回答来自手写脚本，不是模型输出，不代表任何模型能力。

### 5.1 真实 LLM：官方 `e_commerce_33` 任务 0（单次链路演示，不构成评测）

在 3.1 的环境变量之外，这次运行还放宽了两个上限（原因：39 个工具的定义当时在每次 act 调用中出现两次，约 26K token；DeepSeek 的思考 token 计入输出）。Phase 12.5 修复后（ADR-018）默认值已足够，不再需要这两个变量，见 5.3：

```bash
export WORKBENCH_LLM__MAX_TOKENS=8192 WORKBENCH_AGENT__TOKEN_BUDGET=400000
workbench agent run --scenario e_commerce_33 --approve auto --approver claude-code-operator \
  "Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under \$200 to my cart in quantity 1."
```

2026-09-24 的实际输出（节选；回答全文、trace 与审计见 `docs/verification/logs/2026-09-24-phase12-workbench-agent.log`）：

```
session 98f642235e74: 39 tools from http://127.0.0.1:18100/mcp
tool e_commerce_33__search_products -> ok (allowed)
tool e_commerce_33__list_product_offers -> ok (allowed)
approval_required e_commerce_33__add_item_to_cart
approval_granted e_commerce_33__add_item_to_cart
tool e_commerce_33__add_item_to_cart -> ok (allowed)
memory_saved headphone_budget
memory_saved prefers_top_rated
memory_saved cart_id
answer Done! Here's a summary: …
{ "changed": true, ... "cart_items": { "rows_before": 3, "rows_after": 4, "added": [4], ... } }
```

审批由操作者在运行前决定（`--approve auto`）；闸门本身照常工作：写操作前暂停，记录 `approval_requested` / `approval_granted`，网关凭一次性令牌放行。这一次输出只证明链路打通，不代表任务完成得好不好。

### 5.2 上游 `awm agent` / `awm verify` 与轨迹查看器（单次链路演示，不构成评测）

`awm agent` 用 `--mcp_url` 模式连接 env-manager 启动的会话（`--scenario` 自动起服有上游缺陷，见 LIMITATIONS §6）：

```bash
workbench env serve &                                   # 控制面
workbench env up e_commerce_33 --session-id p12awm      # -> http://127.0.0.1:18100/mcp
OPENAI_API_KEY="$DEEPSEEK_API_KEY" awm agent --scenario e_commerce_33 --task_id 0 \
  --tasks_path data/awm1k/gen_tasks.jsonl --mcp_url http://127.0.0.1:18100/mcp \
  --api_url https://api.deepseek.com --model deepseek-flash --output_dir data/p12/awm-agent/e_commerce_33_task_0
```

2026-09-24 的实际输出（节选）：

```
--- Iteration 1/30 ---
Assistant (130 chars): I'll start by listing the available tools in this environment.
<tool_call>
{"name": "list_tools", "arguments": null}
</tool_call>
--- Iteration 2/30 ---
Assistant (360 chars): I'll search for the product with the appropriate filters.
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="call_tool">
…
Tool calls: 0
No tool calls detected - task complete.
```

第 2 轮 DeepSeek 在纯文本中输出了它自己的 DSML 工具调用标记，AWM 只识别 `<tool_call>`，循环在第 2 轮结束，没有写操作（按 D3 如实记录，不改上游）。随后：

```bash
workbench env down p12awm
OPENAI_API_KEY="$DEEPSEEK_API_KEY" OPENAI_BASE_URL=https://api.deepseek.com AWM_SYN_OVERRIDE_MODEL=deepseek-flash \
awm verify --input data/p12/awm-agent/e_commerce_33_task_0 \
  --init_db_path data/p12/awm-runs/p12awm/initial.db --final_db_path data/p12/awm-runs/p12awm/work.db \
  --mode sql --verifier_path data/awm1k/gen_verifier.jsonl
```

实际输出（节选）：

```
Verifier result: reward_type=incomplete
Running LLM judge for sql mode...
LLM judge classification: agent_error
Saved verification result to data/p12/awm-agent/e_commerce_33_task_0/verify.sql.json
```

最后在 UI 的 Trajectory viewer 标签页用文件输入框加载这次的 `trajectory.json`，显示 "AWM trajectory · scenario e_commerce_33 · task 0 · 2 iterations" 和两个步骤。以上输出只证明链路打通，裁判的分类不构成评测，也不得汇总成比率。详见 `docs/verification/2026-09-24-llm-chain.md` §4–6。

### 5.3 Phase 12.5 修复后的确认运行（单次链路演示，不构成评测）

Phase 12.5 的第 1–5 项修复全部完成后，仓库主人追加授权再运行 1 次同一任务，只用来确认修复没有破坏链路。代码为提交 `f135189`，后端 `openai_compat`（DeepSeek `deepseek-flash`）。只设置 3.1 的环境变量，预算与 `max_tokens` 都用默认值（240000 / 8192，ADR-018），不再需要 5.1 的两个覆盖：

```bash
workbench agent run --scenario e_commerce_33 --approve auto --approver claude-code-operator \
  "Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under \$200 to my cart in quantity 1."
```

2026-09-24 18:37 UTC 的实际输出（节选；全文、trace、行级 diff 与审计见 `docs/verification/logs/2026-09-24-phase12.5-workbench-agent.log`）：

```
session 709b8875d89f: 39 tools from http://127.0.0.1:18100/mcp
tool e_commerce_33__search_products -> ok (allowed)
tool e_commerce_33__list_product_offers -> ok (allowed)
approval_required e_commerce_33__get_or_create_active_cart
approval_granted e_commerce_33__get_or_create_active_cart
tool e_commerce_33__get_or_create_active_cart -> ok (allowed)
approval_required e_commerce_33__add_item_to_cart
approval_granted e_commerce_33__add_item_to_cart
tool e_commerce_33__add_item_to_cart -> ok (allowed)
memory_saved headphone_budget_ceiling
memory_saved cart_id
answer Done! Here's a summary: …
{ "changed": true, ... "cart_items": { "rows_before": 3, "rows_after": 4, "added": [4], ... } }
```

链路再次走通：规划 → 两次读 → 两次写（均先审批）→ 回答 → verify。退出码为 0，7 次 LLM 调用，`tokens_used` 93736，没有触发任何终止条件。行级 diff 与 5.1 相同：只有 `cart_items` 新增 1 行（offer 1，数量 1），其余 18 张表不变。

与 5.1 的差别，以及各项修复在这次运行中的表现（都是工程事实）：

- 模型这次多调用了一次 `get_or_create_active_cart`。它是 GET 路由，名称中的动词 `create` 让它按名称就被判为 `write`（ADR-006 的启发式，不是 HTTP 方法下限），所以同样先审批；购物车 1 已存在，`carts` 表没有变化。
- 审计摘要中的时间戳 `2026-09-24T18:37:39.267449` 保持原样，`[PHONE]` 出现 0 次（ADR-016）。
- 每次 act 调用的 token 数为 14515–16675（5.1 为 25764–27741，ADR-018）。
- 带 `tools` 的 act 调用都成功返回（ADR-017）。trace 不记录请求体，所以这次运行本身不能证明 `reasoning_content` 确实被回传了，回传由单测验证。
- 官方 `e_commerce_33` 的 server 以白名单环境启动，39 个工具都能正常提供（ADR-019）。

这次输出只证明修复后链路仍然打通，不代表任务完成得好不好，也不与 5.1 合并成任何比率。费用见 `docs/verification/cost-ledger.md` 第 6 行。

阅读：

- `src/workbench/agent/graph.py`、`agent/nodes/*.py`（尤其 `approve.py` 中的 `interrupt` 与恢复）；
- `src/workbench/agent/guards.py`（步数、重复调用、无变化、token 预算、墙钟）；
- `src/workbench/agent/memory.py`（按来源设写入门槛，ADR-010）；
- `src/workbench/agent/prompts/*.md` 与 `docs/CHANGELOG.md`（prompt 版本）；
- `src/workbench/runtime.py`、`src/workbench/api/app.py`（SSE、审批接口、会话上限、并发保护）；
- `src/workbench/obs/tracing.py`、`obs/metrics.py`；`ui/app.js`；
- 测试：`tests/unit/test_agent_e2e.py`（正常、拒绝后重新规划、重复调用、计划重试、无效计划、无变化 6 种剧本）、`tests/unit/test_api.py`、`tests/integration/test_agent_real_awm.py`。

## 6. 训练循环：smoke 启动器

目标：理解训练如何与 app 隔离（ADR-002），以及为什么只有 smoke（ADR-012）。

```bash
workbench train preflight           # 无 GPU 时多数检查为 fail，这是预期结果
workbench train launch              # 默认只打印计划；--execute 需要 GPU（UNVERIFIED-LOCAL）
```

预期 preflight 输出（开发沙箱，无 GPU、未安装 train 环境）：

```
│ profile       │ ok     │ smoke: Qwen/Qwen3-0.6B (0.6B), LoRA, 3 steps, NO_RESULTS
│ gpu           │ fail   │ nvidia-smi unavailable: UNVERIFIED-LOCAL (needs a CUDA GPU)
│ train-env     │ fail   │ train env not installed/importable (cd train && uv sync) …
│ verl          │ fail   │ AgentFly's nested verl submodule is not initialized …
│ config-keys   │ fail   │ veRL config files not found (verl submodule not initialized)
│ hydra-compose │ fail   │ Hydra compose failed or train env missing …
│ data          │ ok     │ 8 rows in configs/train/smoke_data.json
```

在 GPU 机器上的步骤（UNVERIFIED-LOCAL）：

1. `git -C third_party/AgentFly submodule update --init verl`（该子模块 URL 为 SSH 形式，需要 SSH key 或 `insteadOf` 重写）；
2. `cd train && uv sync`；
3. `workbench train preflight` 全部为 ok 后，执行 `workbench train launch --execute`。产物目录带 `NO_RESULTS` 标记。

阅读：

- `configs/train/smoke.yaml` 与 `configs/train/README.md`：每个 Hydra 键都来自固定 veRL fork 的实际配置（RECON §10 记录了 AgentFly 示例脚本中不存在的四个键）；
- `src/workbench/train/profile.py`（R2 约束：模型 ≤1.7B、必须 LoRA、≤5 step）；
- `src/workbench/train/preflight.py`、`train/launch.py`；
- `train/pyproject.toml`：独立的 train 环境；
- 测试：`tests/unit/test_train.py`（GPU 信息为 mock）。

## 7. 结果与数字纪律

```bash
make check-numbers      # 扫描 README 与 docs 中的性能类数字
make results            # 由 results/registry.yaml 重新生成 docs/RESULTS.md
```

阅读：`results/registry.yaml`、`src/workbench/results/*.py`、`docs/RESULTS.md`、ADR-013。
