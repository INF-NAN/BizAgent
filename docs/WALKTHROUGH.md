# WALKTHROUGH — 学习路线

面向仓库主人。按"环境 → 合成 → 服务 → 网关 → 智能体 → 训练循环"的顺序，每一步给出命令、预期输出和建议阅读的源文件。所有命令在仓库根目录执行，除第 3、6 步中标注 UNVERIFIED-LOCAL 的部分外，都能在 CPU + mock LLM 下运行。下文的"预期输出"摘自开发沙箱中的实际运行（2026-09-24），会话 ID、端口等随每次运行变化。

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

在没有官方数据集的情况下，下面统一使用手写的迷你场景 `tests/fixtures/awm_mini`（按 AWM 数据格式编写的 7 个工具的迷你电商，不是官方数据）。

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
│ mini_e_commerce │ 7     │ 2     │ 3      │

│ session │ scenario        │ state   │ url                    │ tools │
│ wt1     │ mini_e_commerce │ healthy │ http://127.0.0.1:1810… │ 7     │

snapshot before saved
{ "changed": false, "tables_added": [], "tables_removed": [], "tables": {} }
stopped wt1
```

阅读：

- `src/workbench/envs/awm_adapter.py`：如何调用 AWM 建库与启动 server，为什么必须显式传 `--db_path`、`--temp_server_path`、`--output_dir`（`docs/RECON.md` §1）；
- `src/workbench/envs/manager.py`：进程组启动与 `killpg`、健康检查截止时间、排队信号量、空闲回收；
- `src/workbench/envs/snapshot.py`：快照、恢复、按主键的表级 diff；
- `src/workbench/envs/service.py`、`envs/http.py`：本地与远程两种 env 服务；
- 测试：`tests/unit/test_env_manager.py`、`tests/integration/test_env_real_awm.py`（真实启动 AWM server）。

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

- `src/workbench/synth/runner.py`：步骤计划、`state.json` checkpoint、manifest（`origin: local-synth`）、种子文件复制；
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
vllm serve Snowflake/Arctic-AWM-4B --host 127.0.0.1 --port 8000 --served-model-name Snowflake/Arctic-AWM-4B --gpu-memory-utilization 0.9
```

在 GPU 机器上运行 `scripts/serve_vllm.sh`，然后设置 `WORKBENCH_LLM__BACKEND=vllm`（UNVERIFIED-LOCAL：需要 CUDA GPU 和 `huggingface.co` 访问）。

阅读：

- `configs/serving/arctic-awm-4b.yaml`：每个参数都注明了 vLLM v0.19.0 的源码位置；
- `src/workbench/llm/client.py`：httpx 分阶段超时 + `asyncio.timeout` 墙钟，只对网络错误和 5xx 重试；
- `src/workbench/llm/backends/openai_compat.py`（流式 SSE）、`backends/mock_replay.py`（手写脚本回放）；
- `src/workbench/llm/toolcall_parse.py`；
- 测试：`tests/unit/test_llm_client.py`、`tests/unit/test_mock_replay.py`。

## 4. 网关：策略、审批、审计

目标：理解为什么所有工具调用都必须经过网关（ADR-005/006/007/009）。

```bash
workbench gateway export-risk --dataset-dir tests/fixtures/awm_mini --out data/risk_table.csv
```

预期输出：

```
wrote 7 rows to data/risk_table.csv (offline: names only; live sessions add descriptions)

scenario,tool,risk,source,reason,requires_approval
mini_e_commerce,search_products,read,heuristic,verb 'search' => read,False
mini_e_commerce,add_item_to_cart,write,heuristic,verb 'add' => write,True
mini_e_commerce,remove_cart_item,destructive,heuristic,verb 'remove' => destructive,True
...
```

`workbench gateway serve` 可以把网关作为独立的 MCP server 运行，任何 MCP 客户端都能连接。

阅读：

- `configs/tool_policy.yaml`：动词表、未知动词默认按 `write` 处理、需要审批的风险级别、限流参数；
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
