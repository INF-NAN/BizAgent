# 示例：用 DeepSeek 跑通官方 e_commerce_33

本文记录一次真实的端到端运行：OpenAI 兼容后端接 DeepSeek（模型 `deepseek-flash`），在官方场景 `e_commerce_33` 的任务 0 上分别运行 `workbench agent run`，以及上游的 `awm agent` 与 `awm verify`。每条链路只运行了一次，用来展示各组件怎样衔接；输出不代表任务完成质量，也不构成评测（ADR-013）。

数据来源：场景、任务文本、工具名、参数与数据行取自 AgentWorldModel-1K（`Snowflake/AgentWorldModel-1K`，revision `dde80a0`），作者 Zhaoyang Wang, Canwen Xu, Boyi Liu, Yite Wang, Siwei Han, Zhewei Yao, Huaxiu Yao, Yuxiong He，许可证 [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)。本文只做摘录，未修改原数据。

任务 0 的原文：

> Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under $200 to my cart in quantity 1.

## 1. workbench 智能体

配置全部通过进程环境变量设置，key 只从 `DEEPSEEK_API_KEY` 读取，不写入任何文件；预算与 `max_tokens` 使用默认值。

```bash
export WORKBENCH_LLM__BACKEND=openai_compat WORKBENCH_LLM__BASE_URL=https://api.deepseek.com \
       WORKBENCH_LLM__MODEL=deepseek-flash WORKBENCH_LLM__API_KEY_ENV=DEEPSEEK_API_KEY
uv run workbench agent run --scenario e_commerce_33 --approve auto \
  "Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under \$200 to my cart in quantity 1."
```

`--approve auto` 表示由操作者事先决定批准；审批闸门照常工作：图在每次写操作前暂停，记录 `approval_requested` / `approval_granted`，网关凭一次性令牌放行。

env-manager 为会话启动官方 server，显式传入 `--db_path`、`--temp_server_path` 与 `--output_dir`，server 进程只拿到白名单中的环境变量（ADR-017），39 个工具全部可用。智能体的过程（摘自 trace）：

| 步骤 | 事件 |
|---|---|
| intake | 从网关拉取 39 个工具及其风险级别 |
| plan | 计划：按评分搜索 → 查看报价 → 加入购物车（标记为改数据的步骤） |
| act 1 | `search_products {"query": "wireless noise cancelling headphones", "sort_by": "average_rating", "max_price": 200}`，风险 read，直接执行，`ok` |
| act 2 | `list_product_offers {"product_id": 2}`，read，`ok`：评分最高的商品只有一个超出预算的报价 |
| act 3 | `get_or_create_active_cart {}`，GET 路由，但名称中的动词 `create` 让它被判为 write（ADR-006）→ 审批 → 执行，返回已存在的购物车 1 |
| act 4 | `add_item_to_cart {"product_offer_id": 1, "quantity": 1}`，write → 审批 → 执行，返回 `cart_item.id = 4` |
| act 5 | 不再调用工具，给出回答：评分最高的 Bose QuietComfort 35 II 报价高于 200 美元，所以加入的是 Sony WH-CH710N（offer 1） |
| verify | `complete: true`；写入两条长期记忆：预算上限（`user_stated`）与购物车 ID（`tool_result`） |

DB diff 只有 `cart_items` 变化：

```json
{"changed": true, "tables": {"cart_items": {"rows_before": 3, "rows_after": 4, "added": [4], "removed": [], "changed": []}}}
```

行级核对：新增行为 `(id 4, cart_id 1, product_offer_id 1, quantity 1)`，offer 1 属于商品 1 Sony WH-CH710N Wireless Noise Cancelling Headphones；其余 18 张表不变，`carts` 表也没有变化。审计摘要中的微秒时间戳保持原样（ADR-014）。

这次运行所用的版本早于审批前预演（ADR-026）与审批策略（ADR-027）。用当前版本运行时，每次人工审批前会多一个 `preview` 事件，审批请求还带有审批策略的判定。

与 DeepSeek 对接时用到的接口行为：

- 流式响应返回原生 `tool_calls`，按 `index` 分片，最后一个 chunk 带 `usage`，`openai_compat` 后端直接解析；
- 思考模式默认开启，思考 token 计入输出 token，`temperature` 不生效；
- 带 `tools` 的请求按 DeepSeek 文档回传此前各轮的 `reasoning_content`（ADR-015）；
- DeepSeek 接受但忽略 `max_completion_tokens`，只有 `max_tokens` 生效。

## 2. 上游 `awm agent` 与 `awm verify`

`awm agent` 以 `--mcp_url` 模式连接 env-manager 启动的会话，原因见 [UPSTREAM.md](../UPSTREAM.md) §6.4：

```bash
uv run workbench env serve &
uv run workbench env up e_commerce_33 --session-id awmdemo          # 返回会话的 MCP 地址
OPENAI_API_KEY="$DEEPSEEK_API_KEY" uv run awm agent --scenario e_commerce_33 --task_id 0 \
  --tasks_path data/awm1k/gen_tasks.jsonl --mcp_url http://127.0.0.1:18100/mcp \
  --api_url https://api.deepseek.com --model deepseek-flash \
  --output_dir data/awm-agent/e_commerce_33_task_0
```

其余参数为上游默认值（`max_iterations` 30、`temperature` 1.0、`max_tokens` 2048）。过程：

| 轮次 | 模型输出 | AWM 的处理 |
|---|---|---|
| 1 | `<tool_call>{"name": "list_tools", "arguments": null}</tool_call>` | 解析成功，返回 39 个工具的清单 |
| 2 | DeepSeek 自己的工具调用标记 `<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="call_tool">…`，调用 `search_products` | AWM 只识别 `<tool_call>`（`awm/core/agent.py:130-167`），解析出 0 个调用，把这段文本当作最终回答，循环结束 |

会话数据库没有变化（`workbench env diff awmdemo` 的 `changed` 为 false）。`awm agent` 与 DeepSeek 的文本协议在第 2 轮就不再兼容：DeepSeek-V4.1-Flash 的原生工具调用格式是 DSML 标记，而 AWM 的文本协议只接受 `<tool_call>`。本仓库不修改上游，也不为此写适配器。

随后对这次输出运行 `awm verify --mode sql`（官方 verifier + DeepSeek 裁判）：

```bash
uv run workbench env down awmdemo
OPENAI_API_KEY="$DEEPSEEK_API_KEY" OPENAI_BASE_URL=https://api.deepseek.com AWM_SYN_OVERRIDE_MODEL=deepseek-flash \
uv run awm verify --input data/awm-agent/e_commerce_33_task_0 \
  --init_db_path data/runs/awmdemo/initial.db --final_db_path data/runs/awmdemo/work.db \
  --mode sql --verifier_path data/awm1k/gen_verifier.jsonl
```

verifier 执行完成（`reward_type: incomplete`），裁判返回可解析的 JSON，分类为 `agent_error`，结果写入 `verify.sql.json`。执行期间两个数据库被设为只读，结束后权限恢复。这次运行直接调用了 `awm verify`；本仓库推荐经 `workbench verify` 运行，verifier 代码因此拿不到真实 key（ADR-023）。

最后在 UI 的 Trajectory viewer 中加载这次的 `trajectory.json`，页面按 AWM 格式识别，显示 "AWM trajectory · scenario e_commerce_33 · task 0 · 2 iterations" 与两个步骤。
