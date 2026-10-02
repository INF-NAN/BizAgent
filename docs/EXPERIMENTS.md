# 批量实验（lab）

`workbench lab` 把工作台的整条链路（隔离环境、MCP 网关、审批策略与预演、LangGraph 智能体、录制、官方 verifier）放到官方 AgentWorldModel-1K 的大量任务上批量运行，用来回答几个可以被数据检验的问题。`scripts/lab/run_all.sh` 在一台单卡机器上无人值守地跑完全部实验，最后生成 `data/lab/REPORT.md` 与 `data/lab/summary.json`。

本文只写设计与做法，不写结果：结果由报告从运行记录计算，没有运行就没有数字。设计决定的理由见 [DECISIONS.md](DECISIONS.md) ADR-028。

## 要回答的问题

1. 一个 4B 的开源模型（`Qwen/Qwen3-4B`，关闭思考）放进这套工作台，在从未见过的场景上能完成多少任务，失败集中在哪里。
2. 同一模型多次采样能覆盖多少任务（pass@k），即单次结果之外还有多少可挖掘的空间。
3. 用更强的模型（教师，DeepSeek API）的轨迹训练小模型，哪种数据最有效：
   - 只用通过 verifier 的教师轨迹（教师蒸馏）；
   - 在此之上加入学生自己采样、通过 verifier 的轨迹（拒绝采样自提升，RFT / expert iteration）；
   - 不经 verifier 过滤、全部教师轨迹（消融：过滤本身的作用）。
4. 工具结果里被植入的指令（间接 prompt injection）能否让智能体执行破坏性操作；网关的三种配置各挡住多少；训练之后模型是否更容易被诱导。
5. 真实的智能体流量下，推理服务的前缀缓存与多 LoRA 服务各带来什么。
6. 不运行 verifier，能否仅凭运行时可观察的信号判断一个 episode 失败了，从而只把一部分 episode 交给人复核。

## 数据与划分

- 数据：官方 AgentWorldModel-1K，固定修订 `dde80a0283fe781bdc51656bce57063dc5650213`（`AWM1K_REVISION`），只读，不修改。
- 只用带 pure-code verifier 的任务（`gen_verifier.pure_code.jsonl`）。同一任务有多个 verifier 条目时取第一条，与 `awm verify` 的选择一致；选中的条目复制到 `data/lab/verifiers.code.jsonl`，每个 episode 只把自己的那一条写进运行目录，验证时不必重复加载整份 verifier 文件。
- 按场景划分，不按任务划分（`workbench lab split`，`workbench.lab.splits`）：
  - 有 verifier 的场景按固定种子打乱，前 200 个作为 test，接下来 100 个作为 val，其余作为 train；
  - 每个 split 内部再按种子打乱任务，取 test 300、val 100、train 1500 个任务；
  - 训练数据只来自 train 场景。test 上的变化反映的是对未见过的环境（数据库、工具集、业务规则）的泛化，而不是记住了某个场景。
- 划分写入 `data/lab/splits.json`（种子、每个 split 的场景与任务），同样的种子与数据修订得到同样的划分。

## 一个 episode 怎样运行和判定

`workbench lab eval`（`workbench.lab.episodes`）：

- 所有 episode 共用一个 Runtime（环境管理器、网关、LLM 客户端），信号量限制并发；每个 episode 有自己的隔离环境会话、自己的记忆命名空间（`user_id` 为 episode 编号）和自己的 LLM 调用录制。并行的两次运行用不同的端口区间（`--port-min`）。
- 审批：批量运行没有人在场。
  - 默认策略文件是 `configs/lab/eval_policy.yaml`：write 由策略自动批准，不拒绝任何调用，需要删除的任务也能完成；
  - destructive 调用不能被策略自动批准（ADR-027），仍然先预演再进入审批，由本次运行的 approver 代替人回答：`auto` 全部批准，`preview-guard` 只批准预演成功且不删除任何行的调用。
- 判定：智能体结束后，用该任务的官方 pure-code verifier 对比会话的 `initial.db` 与 `work.db`，经 `workbench verify`（ADR-023）运行 `awm verify`。`reward_type` 为 `complete` 记为成功，其余都是失败。
- 每个 episode 在 `data/lab/runs/<tag>/results.jsonl` 写一行，内容包括：
  - 结束方式与最终回答；
  - LLM 调用次数与 token、步数；
  - 工具调用、工具错误与空结果、写操作、被拒绝的调用、每次审批（工具、风险、命中的规则、预演摘要、批准与否）；
  - 实际的数据库改动（每张表增删改的行数）、verifier 结论、耗时。
- 录制：每次 LLM 调用的完整请求（消息、工具）与回复写入 `runs/<tag>/calls/<episode>.json.gz`，供 SFT 数据与推理服务回放使用。
- 可恢复：已经写入的 episode 再次运行时跳过。环境启动失败、verifier 未能运行这类基础设施错误会被移出文件并重跑。`--min-ok` 在出错的 episode 过多时让命令以非零状态退出，一键脚本因此会停下，而不是在坏掉的环境上继续跑。
- 运行完成后删除会话目录，只留下结果行与录制；`--keep-envs` 保留数据库与 trace。

失败分类（`workbench.lab.metrics.failure_category`）：每个失败的 episode 按顺序归入一类：

1. 基础设施错误；
2. LLM 错误；
3. 计划不合法；
4. 守卫终止（最大步数、重复调用、状态无变化、token 预算、时钟）；
5. 没有调用工具；
6. 工具报错；
7. 没有写操作；
8. 有写操作但结果不对。

## 实验

| 运行（tag） | 模型 | split | 说明 |
|---|---|---|---|
| `base-test`、`base-val` | Qwen3-4B | test、val | 温度 0，每个任务一次 |
| `base-passk` | Qwen3-4B | test 前 150 个任务 | 温度 0.7，每个任务 4 次，pass@1、pass@2、pass@4 |
| `teacher-train` | DeepSeek | train | 教师轨迹，训练数据来源 |
| `teacher-test` | DeepSeek | test | 参考上界 |
| `student-train` | Qwen3-4B | train | 温度 0.7，每个任务 2 次，拒绝采样的候选 |
| `sft-<variant>-val-<ckpt>` | LoRA 各 checkpoint | val | 只用来选 checkpoint |
| `sft-<variant>-test` | 选中的 checkpoint | test | 每个变体只评一次 |
| `inj-a`、`inj-b`、`inj-c` | Qwen3-4B | test 前 150 个任务 | 注入实验的三种网关配置 |
| `inj-sft-a` | val 上最好的 LoRA | test 前 150 个任务 | 训练后的模型，配置 A |

### pass@k

`base-passk` 每个任务采样 n = 4 次，用无偏估计 pass@k = 1 − C(n−c, k) / C(n, k)（c 为成功次数）对任务取平均。pass@4 与 pass@1 的差距，就是拒绝采样能利用的空间：模型偶尔能做对的任务，自己的成功轨迹就能成为训练数据。

### 训练：三种数据，同一套训练

数据（`workbench lab sft-data`，`workbench.lab.sft_data`）：

- 一个 episode 的每次 LLM 调用都是一对（消息，回复）。act 调用逐步延长同一段对话，所以一次调用的对话若是后面某次调用的前缀就丢弃，只保留最长的那段；plan 与 verify 调用是独立的对话，原样保留。
- 每条样本带上请求时发送的工具列表，格式与发给推理服务的请求相同（`_tools_payload`）。
- 三个变体：

  | 变体 | 来源 | 过滤 |
  |---|---|---|
  | `teacher` | `teacher-train` | 只用 verifier 判定成功的 episode |
  | `rft` | `teacher-train` 与 `student-train` | 只用成功的 episode；每个任务最多 2 个，教师优先，避免容易的任务占满数据 |
  | `unfiltered` | `teacher-train` | 所有正常结束的 episode，不看 verifier |

训练（`scripts/lab/sft_train.py`，在 GPU 环境中运行）：

- 从 `Qwen/Qwen3-4B` 出发，三个变体各训练一个 LoRA（rank 64、alpha 128，作用于全部线性层），bf16、梯度检查点。学习率 1e-4、cosine 调度、带 warmup，每步 16 条序列，2 个 epoch，每半个 epoch 存一个 checkpoint。其余超参见脚本参数的默认值。
- 样本用模型自己的 chat template 和同一份工具列表渲染，与 vLLM 渲染请求的方式相同；工具调用的参数先解析为对象，模板输出的 JSON 与推理时一致。
- 只在 assistant 轮次上计算 loss：从 `<|im_start|>assistant\n` 之后到 `<|im_end|>`（含）。
- 推理时关闭思考，每次生成都从一个空的 `<think>\n\n</think>\n\n` 之后开始；模板只给最后一轮写这个块，所以训练时在每个更早的 assistant 轮次前也插入它（不计 loss），保证每一轮的学习目标紧跟在推理时它前面的那段文本之后。代价是这些更早的轮次在后续轮次的上下文里多出这个空块，而推理时的历史里没有。已用 Qwen3-4B 的 tokenizer 核对过渲染与掩码。
- 只在需要 loss 的位置计算 logits（`logits_to_keep` 传位置索引），超长样本（默认超过 24576 token）丢弃并计数。

选择与评测：

- 一个 vLLM 进程以多 LoRA 方式同时服务全部 checkpoint（`--enable-lora --lora-modules ...`），每个 checkpoint 在 val 上跑一遍；每个变体选 val 成功率最高的 checkpoint（并列取更早的），只用它在 test 上评一次。test 不参与任何选择。
- 对比都在同一批 test 任务上配对进行：base 与每个变体、`teacher` 与 `rft`（自提升的作用）、`unfiltered` 与 `teacher`（过滤的作用）。每组报告仅一方成功的任务数、McNemar 精确检验的 p 值，以及成功率差的配对 bootstrap 95 区间（5000 次重采样）。

### 工具结果注入

`InjectingGateway` 包在真实网关外面：每个 episode 第一次成功的读调用，其返回的记录里多出一个字段，内容是一条要求调用本场景某个 destructive 工具的指令，工具与措辞（5 种模板）由 episode 编号确定性地选出。除此之外一切都交给真实网关，策略、审批、预演照常生效。

| 配置 | 网关 | 审批方 |
|---|---|---|
| A | `eval_policy.yaml` | 全部批准，相当于没有防护 |
| B | `deny_destructive.yaml`：任何 destructive 调用都被规则拒绝 | 全部批准 |
| C | `eval_policy.yaml` | `preview-guard`：只批准预演中不删除任何行的调用 |

指标：attempted（模型发起了被要求的调用）、executed（该调用真正执行成功），以及注入下的任务成功率，用来观察防护对正常任务的代价。A 与 `inj-sft-a` 对比，看训练是否改变了模型对注入的服从程度。

### 推理服务

`workbench lab bench`（`workbench.lab.serving_bench`）回放 `base-test` 录下的真实请求：每个模拟客户端按顺序重放一个 episode 的全部调用，与智能体发送请求的方式相同，后一次请求的 prompt 延续前一次。输出长度固定（`max_tokens` 256，`ignore_eos`），`tool_choice` 为 `none`：工具仍渲染进 prompt，但不解析输出，各配置之间只有服务端设置不同。并发 1、8、32，客户端数等于并发，episode 数为并发的两倍且至少 8 个。

记录 TTFT 与端到端延迟的 p50、p95、输出 token 吞吐、请求吞吐，以及从 vLLM `/metrics` 读取的前缀缓存命中率（运行前后计数之差）。对比四组：

- 前缀缓存开与关；
- 开启 LoRA 的服务上，用 LoRA adapter 与用基座模型。

### 无 verifier 的失败预测

`scripts/lab/risk_model.py`（GPU 环境中的 scikit-learn）：

- 特征只用运行时看得到的信号：步数、工具调用、工具错误与空结果、结束方式、审批与拒绝、数据库改动的表数与行数、最终回答的长度和其中表示失败的措辞。
- 标签是 verifier 的判定（失败为正类）。
- 按场景分组做 5 折交叉验证（GroupKFold），同一场景不会同时出现在拟合与评分两侧。
- 报告逻辑回归与梯度提升树的 AUROC、AUPRC，以及按风险分数复核前 10、20、30（单位为百分之一）的 episode 时召回的失败比例；同时给出三条不用拟合的单一规则（有工具报错、被守卫终止、没有写操作）作对照。

## 一键运行

在一台 Linux、单张 CUDA GPU 的机器上（按显存 96 GB 设计，例如 AutoDL 的 RTX PRO 6000），把仓库克隆到数据盘，然后：

```bash
export DEEPSEEK_API_KEY=...        # 教师模型；脚本不把 key 写入任何文件
mkdir -p data/lab
nohup bash scripts/lab/run_all.sh > data/lab/run.log 2>&1 &
tail -f data/lab/run.log
```

脚本依次完成：

1. 检查与准备：
   - 检查 GPU、key、磁盘空间（默认要求至少 60 GB 可用，`MIN_FREE_GB` 可改）；
   - 安装 uv，同步应用环境；
   - 在 `data/lab/.venv-gpu` 建立 GPU 环境（`scripts/lab/gpu-requirements.txt`：vLLM、transformers、peft、scikit-learn，版本与 train 环境的锁一致，但不装 AgentFly 与 veRL）；
   - 下载数据集与基座模型。

   AutoDL 上若存在 `/etc/network_turbo` 会先启用它，并默认使用 Hugging Face 镜像。
2. 划分数据。
3. 教师在后台运行（只占 API，不占 GPU）：先 `teacher-train`，再 `teacher-test`。
4. 前台在 GPU 上运行 base 模型：
   - 冒烟（val 的 8 个任务，出错过多即停止）；
   - `base-test`、`base-val`、`base-passk`、`student-train`；
   - 注入实验 A、B、C；
   - 前缀缓存开、关两组服务压测。
5. 等待教师完成，生成三份 SFT 数据，依次训练三个 LoRA。
6. 启动多 LoRA 服务：各 checkpoint 在 val 上选择 → 各变体的 test → 最好变体的注入实验 → LoRA 服务压测。
7. 失败预测模型与报告。

可恢复：每个阶段完成后写 `data/lab/.stages/<阶段>.done`，再次运行同一命令时跳过已完成的阶段，评测从 `results.jsonl` 断点继续。每个阶段的日志在 `data/lab/logs/`。可选环境变量见脚本开头：`BASE_MODEL`、`TEACHER_MODEL`、`TEACHER_BASE_URL`、`CONC`、`TEACHER_CONC`、`TEACHER_TRAIN_LIMIT`、`SKIP_SETUP`。

耗时与花费（估计，不是测量）：

- 评测的主要成本是 episode 数：base 模型共约 5000 个 episode，三个变体在 val 与 test 上约 2100 个，注入实验 600 个。
- 三次 LoRA 训练各自取决于数据量。
- 教师的 1800 个 episode 与 GPU 阶段并行，费用由 token 量决定。每次运行的 prompt 与 completion token 总量都记在 `results.jsonl`，可以按 `configs/pricing.yaml` 的价格自行核算；想少花，用 `TEACHER_TRAIN_LIMIT` 只让教师跑一部分训练任务。

## 输出

```
data/lab/
├── splits.json, verifiers.code.jsonl   # 划分与选中的 verifier 条目
├── runs/<tag>/                         # meta.json、results.jsonl、calls/*.json.gz、audit.jsonl
├── sft/<variant>/                      # train.jsonl、train.stats.json、adapters/ckpt-*/、selected.json
├── bench/<label>.json                  # 推理服务压测
├── risk/risk.json                      # 失败预测
├── logs/, .stages/                     # 阶段日志与完成标记
├── summary.json                        # 报告用到的全部数字
└── REPORT.md                           # 报告
```

`data/` 不进入 git。要把某个结果写进 README 或 docs，先登记到 `results/registry.yaml`，`make check-numbers` 会拦截未登记的数字（ADR-013）。

## 局限

- 每个配置只跑一个种子，结论的不确定性只来自任务抽样（Wilson 区间、配对 bootstrap），没有包含训练随机性。
- 只有一个 4B 学生模型与一个教师模型。
- verifier 是上游按任务自动生成的代码，本身会误判；报告中的成功率是"被该 verifier 判为 complete"的比例，失败预测的标签也继承了这一点。
- 注入只有一种位置（第一次读调用的返回）与 5 种模板，只代表一类攻击，不代表所有攻击。
- 训练时更早轮次前的空思考块与推理时的历史不完全一致（见上文"训练"）。
- 教师轨迹不包含教师的思考内容，学生学到的是教师可见的回复与工具调用。
