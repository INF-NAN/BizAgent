# ARCHITECTURE — 分层结构与关键时序

本仓库只做应用层与工程层：把上游 AWM 的环境、上游 AgentFly 的训练框架，组织成一个"可部署、可审计、可演示"的企业 MCP 智能体工作台。模型本身和评测都不在范围内（ADR-013）。

## 1. 分层图

```mermaid
flowchart TB
  subgraph APP["应用层"]
    UI["ui/ 静态页面<br/>会话 · 对话 · 审批（含预演）· 时间线 · DB diff · 轨迹查看"]
    API["api/app.py<br/>FastAPI + SSE"]
    AGENT["agent/<br/>LangGraph: intake→plan→act→preview→approve→observe→verify→respond"]
    MEM["agent/memory.py<br/>SqliteStore，按来源设写入门槛"]
    OBS["obs/<br/>trace JSONL · Prometheus"]
  end
  subgraph SERVE["服务层"]
    LLM["llm/client.py<br/>两层超时 · 重试 · &lt;tool_call&gt; 解析"]
    MOCK["mock_replay<br/>手写脚本（CPU）"]
    VLLM["vLLM / OpenAI 兼容端点<br/>（GPU：UNVERIFIED-LOCAL）"]
  end
  subgraph GW["网关层"]
    GATE["gateway/core.py + server.py<br/>MCP server：路由 · deny-first 策略 · 审批策略（approval_policy.yaml） · 预演记录 · 一次性审批令牌 · 执行后比对 · 限流 · 审计 · 空/错归一"]
  end
  subgraph ENV["环境层"]
    MGR["envs/manager.py + service.py<br/>env-manager：每会话独立 DB · 进程组 · 环境变量白名单 · 快照/diff · 审批前预演 · 回收"]
    AWMSRV["AWM MCP server 子进程<br/>third_party/agent-world-model（只读）"]
    DB[("会话 SQLite<br/>initial.db / work.db")]
    SHADOW["影子 AWM server（ADR-029）<br/>会话 DB 的在线备份副本 · 隔离端口 · 用完即回收"]
  end
  subgraph OFF["合成与训练（离线）"]
    SYN["synth/<br/>编排 awm gen 各步 · checkpoint · LLM 代理（缓存/重试/账本） · 校验"]
    TRAIN["train/<br/>preflight · smoke launch（子进程进入独立 train 环境）"]
    AF["third_party/AgentFly + veRL fork<br/>（GPU：UNVERIFIED-LOCAL）"]
  end

  UI -->|HTTP / SSE| API --> AGENT
  AGENT --> MEM
  AGENT --> OBS
  AGENT --> LLM
  LLM --> MOCK
  LLM --> VLLM
  AGENT -->|工具调用| GATE
  GATE -->|MCP Streamable HTTP| AWMSRV --> DB
  API -->|创建/关闭会话、diff| MGR
  MGR -->|启动 / killpg| AWMSRV
  MGR --> DB
  GATE -->|预演 / checkpoint / 实际改动| MGR
  MGR -->|预演时启动 / killpg| SHADOW
  SYN -->|写入 data/synth/，origin: local-synth| MGR
  TRAIN --> AF
```

说明：

- 外部 MCP 客户端也可以直接连接网关（API 内挂载在 `/gateway/mcp`，或 `workbench gateway serve` 独立运行），享有同样的策略与审计（ADR-005）。
- env-manager 可以与 API 同进程（`LocalEnvService`），也可以作为独立服务（`workbench env serve` + `RemoteEnvService`），docker compose 使用后者。
- app 环境与 train 环境是两个独立的 uv 环境；app 代码不 import 任何训练依赖（ADR-002，R11）。

## 2. 一次带审批的写操作

以 demo 场景为例："把 200 美元以内评分最高的无线降噪耳机加入购物车"。

```mermaid
sequenceDiagram
  autonumber
  actor U as 用户（UI）
  participant API as API（SSE）
  participant G as LangGraph 智能体
  participant L as LLM 客户端
  participant GW as 网关
  participant M as env-manager
  participant S as 影子 AWM server（DB 副本）
  participant E as AWM MCP server（会话独立 DB）

  U->>API: POST /sessions/{sid}/messages
  API->>G: run(thread_id=sid)
  G->>L: plan（prompts/plan.md）
  L-->>G: JSON 计划：search → add_to_cart
  G->>L: act
  L-->>G: tool_call search_products
  G->>GW: call_tool（风险 = read）
  GW->>E: tools/call
  E-->>GW: 结果
  GW-->>G: status=ok（审计一条）
  G->>L: act
  L-->>G: tool_call add_item_to_cart
  G->>GW: approval_verdict（审批策略：规则按顺序匹配，ADR-030）
  GW-->>G: require_human（没有命中规则：write 默认需要人工审批）
  G->>GW: preview（preview 节点，ADR-029）
  GW->>M: preview(sid, add_item_to_cart, 参数, 超时)
  M->>M: 在线备份会话当前的 work.db → 影子副本
  M->>S: 启动（独立进程组、租用端口）
  M->>S: tools/call add_item_to_cart
  S-->>M: 结果
  M->>M: diff 副本前后 → 将要改动的行；回收进程组、端口与目录
  M-->>GW: 预演结果
  GW-->>G: 签名的预演记录（digest，是否必需、能否批准）· 审计一条
  G-->>API: interrupt(approval_required + preview)
  API-->>U: SSE: approval_required（工具、参数、风险、将要改动的行）
  U->>API: POST /approvals/{sid} {approved: true, approver}
  API->>G: Command(resume=决定)
  G->>GW: issue_approval（HMAC，绑定会话+工具+参数摘要+预演 digest，一次性）
  G->>GW: call_tool(..., approval_token)
  GW->>GW: 校验令牌并作废 · 限流
  GW->>M: checkpoint（真实调用前的快照）
  GW->>E: tools/call add_item_to_cart
  E-->>GW: 结果
  GW->>M: changes_since(checkpoint)
  M-->>GW: 实际改动
  GW->>GW: 与预演做结构比对 · 审计（审批人、preview_check）
  GW-->>G: status=ok，preview_check=match
  G->>L: act
  L-->>G: 文字回答（无工具调用）
  G->>L: verify（是否完成、可写入的记忆）
  G-->>API: respond（最终回答）
  API-->>U: SSE: done
  U->>API: GET /sessions/{sid}/diff
  API-->>U: cart_items 新增一行
```

要点：

- 被中断的 `approve` 节点在恢复时会重新执行，所以令牌在恢复后才签发（`agent/nodes/approve.py`）；预演也因此放在单独的 `preview` 节点，结果存进 checkpoint，只执行一次。
- 审批策略（ADR-030）：act 先问网关这次调用的决策，只有 require_human 走上图的预演与审批；auto_approve 由网关以 `policy:<规则编号>` 自己签发令牌（不预演，执行后照常测量改动，D32）；deny 由网关直接拒绝。网关在每次调用时按同一策略执行，外部 MCP 客户端也不例外。
- 预演（ADR-029）：影子环境从会话**当前**数据库的副本启动，绝不写会话自己的数据库；有超时；结束后进程组、端口与目录全部回收。
- 令牌与参数摘要、预演 digest 绑定：模型在审批后改动参数，网关会拒绝调用；预演未成功而该风险级别要求预演（`approval.require_preview`，默认 destructive）时不签发令牌，只能拒绝；不要求时令牌绑定 `preview_unavailable`，UI 标出"未预演"。
- 真实执行后按结构比对实际改动与预演（表、主键、改动的列名；时间列只记录不比对），结果写入审计；不一致时记 `preview_mismatch`，UI 标出。
- 拒绝时，智能体收到 `{"status": "rejected"}` 的工具消息并回到 `plan` 重新规划。
- 每一步都会写入 trace（`obs/tracing.py`）并通过 SSE 推给 UI；网关另写一份 JSONL 审计日志（PII 脱敏）。

## 3. 智能体状态机

```mermaid
stateDiagram-v2
  [*] --> intake
  intake --> plan
  plan --> act
  plan --> respond: 计划无效且重试用尽
  act --> preview: 审批策略判定 require_human（write / destructive 的默认）
  preview --> approve: 预演记录（将要改动的行，或失败原因；只读调用不预演）
  act --> observe: 只读工具，或 auto_approve / deny（由网关执行）
  act --> verify: 模型给出文字回答（无工具调用）
  approve --> observe: 批准（携带令牌）
  approve --> plan: 拒绝，或必需的预演未成功（重新规划）
  observe --> act
  verify --> act: 仍有缺项（最多补一轮）
  verify --> respond
  respond --> [*]
```

守卫（`agent/guards.py`）：最大步数、重复调用、连续无变化、token 预算、墙钟时间。任一触发都进入 `respond`（`agent/nodes/common.py`），并在 trace 中记录 `terminated` 原因。长期记忆只在 `verify` 节点写入，且只接受 `user_stated` 与 `tool_result` 两种来源（ADR-010）。
