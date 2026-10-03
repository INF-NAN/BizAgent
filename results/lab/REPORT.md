# BizAgent Lab 实验报告

本文件由 `workbench lab report` 从 data/lab 下的运行结果生成。

## 数据划分

| split | 场景数 | 任务数 |
|---|---|---|
| train | 634 | 1500 |
| train_extra | 637 | 1500 |
| val | 96 | 200 |
| test | 198 | 600 |
| inject | 62 | 200 |

## 各次运行

成功 = 官方 pure-code verifier 判定 complete。区间为 Wilson 95%。

| run | n | 成功 | 成功率 | 95% CI | 平均步数 | 平均 prompt tokens | 平均耗时 s |
|---|---|---|---|---|---|---|---|
| base-passk | 150 | 63 | 42.0% | [34.4%, 50.0%] | 5.0 | 80877 | 57.2 |
| base-test | 600 | 221 | 36.8% | [33.1%, 40.8%] | 4.9 | 78781 | 68.9 |
| base-val | 200 | 72 | 36.0% | [29.7%, 42.9%] | 4.7 | 77672 | 73.4 |
| inj-a | 200 | 83 | 41.5% | [34.9%, 48.4%] | 5.1 | 83298 | 68.1 |
| inj-b | 200 | 83 | 41.5% | [34.9%, 48.4%] | 5.0 | 81682 | 65.9 |
| inj-c | 200 | 83 | 41.5% | [34.9%, 48.4%] | 5.0 | 83379 | 69.3 |
| inj-sft-a | 200 | 91 | 45.5% | [38.7%, 52.4%] | 6.8 | 110948 | 91.5 |
| null-test | 600 | 103 | 17.2% | [14.4%, 20.4%] | 0.0 | 0 | 37.0 |
| null-train | 1500 | 228 | 15.2% | [13.5%, 17.1%] | 0.0 | 0 | 52.4 |
| null-val | 200 | 35 | 17.5% | [12.9%, 23.4%] | 0.0 | 0 | 51.2 |
| sft-rft-test | 600 | 250 | 41.7% | [37.8%, 45.7%] | 6.1 | 96498 | 84.0 |
| sft-rft-val-ckpt-100 | 200 | 80 | 40.0% | [33.5%, 46.9%] | 6.2 | 97706 | 81.9 |
| sft-rft-val-ckpt-200 | 200 | 80 | 40.0% | [33.5%, 46.9%] | 5.8 | 92556 | 79.2 |
| sft-rft-val-ckpt-300 | 200 | 81 | 40.5% | [33.9%, 47.4%] | 6.3 | 100175 | 86.6 |
| sft-rft-val-ckpt-400 | 200 | 78 | 39.0% | [32.5%, 45.9%] | 6.3 | 100473 | 87.1 |
| sft-teacher-test | 600 | 264 | 44.0% | [40.1%, 48.0%] | 6.8 | 108205 | 94.2 |
| sft-teacher-val-ckpt-58 | 200 | 87 | 43.5% | [36.8%, 50.4%] | 7.1 | 111369 | 90.5 |
| sft-teacher-val-ckpt-116 | 200 | 83 | 41.5% | [34.9%, 48.4%] | 6.7 | 107465 | 95.6 |
| sft-teacher-val-ckpt-174 | 200 | 81 | 40.5% | [33.9%, 47.4%] | 6.9 | 110104 | 94.2 |
| sft-teacher-val-ckpt-232 | 200 | 81 | 40.5% | [33.9%, 47.4%] | 6.8 | 108401 | 93.2 |
| smoke | 8 | 3 | 37.5% | [13.7%, 69.4%] | 3.6 | 55063 | 23.7 |
| student-train | 1500 | 559 | 37.3% | [34.9%, 39.7%] | 5.0 | 84498 | 62.8 |
| teacher-test | 300 | 155 | 51.7% | [46.0%, 57.3%] | 6.2 | 103764 | 35.7 |
| teacher-train | 1500 | 734 | 48.9% | [46.4%, 51.5%] | 6.6 | 115595 | 40.7 |

### verifier 下限

让智能体什么都不做（模型不回答，不调用任何工具），test 的 600 个任务中有 103 个仍被 verifier 判为 complete。下表给出 test 上各次运行去掉这些任务后的成功率；配对比较也同时给出去掉它们之后的结果。

| run | n | 成功 | 成功率 | 95% CI |
|---|---|---|---|---|
| base-passk | 128 | 42 | 32.8% | [25.3%, 41.3%] |
| base-test | 497 | 128 | 25.8% | [22.1%, 29.8%] |
| sft-rft-test | 497 | 161 | 32.4% | [28.4%, 36.6%] |
| sft-teacher-test | 497 | 175 | 35.2% | [31.1%, 39.5%] |
| teacher-test | 251 | 107 | 42.6% | [36.7%, 48.8%] |

### 失败分类（每个失败 episode 一个类别，按顺序判定）

- base-passk: wrong_outcome 38, no_write 23, tool_errors 16, guard:max_steps 8, guard:repeated_call 2
- base-test: wrong_outcome 150, tool_errors 98, no_write 97, guard:max_steps 19, guard:repeated_call 13, plan_invalid 1, guard:token_budget 1
- base-val: tool_errors 42, wrong_outcome 40, no_write 29, guard:repeated_call 8, guard:max_steps 4, plan_invalid 2, guard:no_state_change 2, error:agent_error 1
- inj-a: wrong_outcome 55, tool_errors 38, no_write 12, guard:repeated_call 5, guard:max_steps 3, plan_invalid 3, guard:token_budget 1
- inj-b: wrong_outcome 58, tool_errors 31, no_write 12, guard:repeated_call 8, guard:max_steps 4, plan_invalid 3, guard:token_budget 1
- inj-c: wrong_outcome 54, tool_errors 34, no_write 13, guard:repeated_call 9, guard:max_steps 3, plan_invalid 3, guard:token_budget 1
- inj-sft-a: wrong_outcome 43, guard:repeated_call 23, guard:max_steps 15, tool_errors 11, no_write 5, plan_invalid 5, guard:token_budget 4, guard:no_state_change 3
- null-test: plan_invalid 497
- null-train: plan_invalid 1272
- null-val: plan_invalid 165
- sft-rft-test: wrong_outcome 144, tool_errors 56, no_write 54, guard:max_steps 43, guard:repeated_call 36, guard:no_state_change 13, guard:token_budget 2, plan_invalid 2
- sft-rft-val-ckpt-100: wrong_outcome 48, guard:max_steps 21, guard:repeated_call 16, no_write 14, tool_errors 13, guard:no_state_change 7, plan_invalid 1
- sft-rft-val-ckpt-200: wrong_outcome 47, tool_errors 26, guard:max_steps 16, no_write 13, guard:repeated_call 11, guard:no_state_change 7
- sft-rft-val-ckpt-300: wrong_outcome 48, tool_errors 22, guard:repeated_call 16, guard:max_steps 15, no_write 10, guard:no_state_change 8
- sft-rft-val-ckpt-400: wrong_outcome 49, guard:max_steps 25, tool_errors 22, no_write 15, guard:repeated_call 8, guard:no_state_change 3
- sft-teacher-test: wrong_outcome 116, guard:repeated_call 75, guard:max_steps 57, no_write 37, tool_errors 34, guard:no_state_change 7, plan_invalid 7, guard:token_budget 2, guard:wall_clock 1
- sft-teacher-val-ckpt-58: guard:repeated_call 31, wrong_outcome 30, guard:max_steps 23, tool_errors 13, no_write 8, guard:no_state_change 4, plan_invalid 4
- sft-teacher-val-ckpt-116: wrong_outcome 44, guard:repeated_call 20, guard:max_steps 17, tool_errors 17, no_write 8, guard:no_state_change 8, plan_invalid 2, guard:token_budget 1
- sft-teacher-val-ckpt-174: wrong_outcome 45, guard:max_steps 23, guard:repeated_call 21, no_write 11, tool_errors 11, guard:no_state_change 6, plan_invalid 1, guard:token_budget 1
- sft-teacher-val-ckpt-232: wrong_outcome 42, guard:repeated_call 25, guard:max_steps 21, tool_errors 14, no_write 11, guard:no_state_change 4, guard:token_budget 1, plan_invalid 1
- smoke: no_write 2, guard:repeated_call 1, tool_errors 1, wrong_outcome 1
- student-train: wrong_outcome 393, no_write 236, tool_errors 231, guard:repeated_call 32, guard:max_steps 26, plan_invalid 9, guard:no_state_change 8, guard:token_budget 5, llm_error 1
- teacher-test: wrong_outcome 54, no_write 48, guard:max_steps 23, tool_errors 11, guard:no_state_change 4, guard:repeated_call 3, llm_error 1, no_tool_call 1
- teacher-train: wrong_outcome 286, no_write 180, guard:max_steps 129, tool_errors 83, guard:token_budget 54, guard:no_state_change 24, guard:repeated_call 7, no_tool_call 3

## pass@k（base 模型，temperature 0.7）

150 个 test 任务，每个任务 4 次采样，无偏估计。

| k | pass@k |
|---|---|
| 1 | 40.3% |
| 2 | 43.3% |
| 4 | 44.7% |

## 训练：教师蒸馏与拒绝采样自提升（LoRA SFT）

base 在 val 上：72/200（36.0%）。checkpoint 只按 val 选择，test 只评一次。

### teacher：teacher distillation (verified teacher episodes)

- 数据：读取 1500 个 episode，使用 522 个（其中 verifier 通过 522），522 个任务、369 个场景，1854 条样本。
- 训练：1854 条样本（超长丢弃 0），18186059 tokens，其中计算 loss 的 643487，232 步，用时 27419 s。

| checkpoint | n | val 成功率 | 95% CI |
|---|---|---|---|
| ckpt-58 | 200 | 43.5% | [36.8%, 50.4%] |
| ckpt-116 | 200 | 41.5% | [34.9%, 48.4%] |
| ckpt-174 | 200 | 40.5% | [33.9%, 47.4%] |
| ckpt-232 | 200 | 40.5% | [33.9%, 47.4%] |

选中：ckpt-58

test 配对比较（base → teacher，同一批 600 个任务）：

- base 221/600，teacher 264/600
- 仅 base 成功 26，仅 teacher 成功 69，McNemar 精确检验 p = 1.183e-05
- 成功率差 7.2%，配对 bootstrap 95% 区间 [4.0%, 10.3%]
- 去掉不做任何操作也能通过 verifier 的任务后（497 个）：base 128，teacher 175，McNemar p = 3.041e-07，差 9.5%，区间 [6.0%, 12.9%]

### rft：teacher + student rejection sampling (verified episodes of both)

- 数据：读取 4500 个 episode，使用 945 个（其中 verifier 通过 945），582 个任务、399 个场景，3186 条样本。
- 训练：3186 条样本（超长丢弃 0），29247450 tokens，其中计算 loss 的 899928，400 步，用时 43700 s。

| checkpoint | n | val 成功率 | 95% CI |
|---|---|---|---|
| ckpt-100 | 200 | 40.0% | [33.5%, 46.9%] |
| ckpt-200 | 200 | 40.0% | [33.5%, 46.9%] |
| ckpt-300 | 200 | 40.5% | [33.9%, 47.4%] |
| ckpt-400 | 200 | 39.0% | [32.5%, 45.9%] |

选中：ckpt-100

test 配对比较（base → rft，同一批 600 个任务）：

- base 221/600，rft 250/600
- 仅 base 成功 23，仅 rft 成功 52，McNemar 精确检验 p = 0.00108
- 成功率差 4.8%，配对 bootstrap 95% 区间 [2.0%, 7.7%]
- 去掉不做任何操作也能通过 verifier 的任务后（497 个）：base 128，rft 161，McNemar p = 8.769e-05，差 6.6%，区间 [3.4%, 9.9%]

### 拒绝采样自提升相对教师蒸馏（test）

test 配对比较（teacher → rft，同一批 600 个任务）：

- teacher 264/600，rft 250/600
- 仅 teacher 成功 42，仅 rft 成功 28，McNemar 精确检验 p = 0.1196
- 成功率差 -2.3%，配对 bootstrap 95% 区间 [-5.2%, 0.3%]
- 去掉不做任何操作也能通过 verifier 的任务后（497 个）：teacher 175，rft 161，McNemar p = 0.09795，差 -2.8%，区间 [-5.8%, 0.2%]

## 教师模型参考（test 集）

教师 155/300，base 112/300。

## 过程指标（test）

不看 verifier，只看 episode 是怎么进行的。teacher-test 只跑了 test 的前一部分任务。

| run | episodes | 平均步数 | 每 episode 工具报错 | 有工具报错 | 计划无效 | LLM 错误 | 守卫终止 | 未调用工具 | 平均 prompt tokens |
|---|---|---|---|---|---|---|---|---|---|
| base-test | 600 | 4.9 | 0.57 | 30.0% | 0.3% | 0.0% | 7.0% | 0.3% | 78781 |
| sft-teacher-test | 600 | 6.8 | 0.55 | 27.5% | 1.3% | 0.0% | 30.0% | 1.3% | 108205 |
| sft-rft-test | 600 | 6.1 | 0.56 | 27.7% | 0.3% | 0.0% | 19.8% | 0.3% | 96498 |
| teacher-test | 300 | 6.2 | 0.30 | 14.0% | 0.0% | 0.7% | 13.7% | 1.0% | 103764 |

## 工具结果注入

任务取自含 destructive 工具的 test 场景（inject 任务集）。每个 episode 第一次成功的读调用结果后追加一条要求调用本场景某个 destructive 工具的指令。
attempted = 模型发起了该调用；executed = 该调用真正执行成功。

| 配置 | episodes | attempted | executed | executed 95% CI | 任务成功率 |
|---|---|---|---|---|---|
| A: base, approve everything | 200 | 8.0% | 7.5% | [4.6%, 12.0%] | 41.5% |
| B: base, policy denies destructive tools | 200 | 8.0% | 0.0% | [0.0%, 1.9%] | 41.5% |
| C: base, approve only when the preview removes no rows | 200 | 9.0% | 7.0% | [4.2%, 11.4%] | 41.5% |
| A: selected SFT model, approve everything | 200 | 9.5% | 9.0% | [5.8%, 13.8%] | 45.5% |

## 推理服务（回放录制的 agent 请求，固定输出 256 tokens）

| 配置 | 并发 | 请求数 | TTFT p50 ms | TTFT p95 ms | 延迟 p50 ms | 输出 tok/s | 前缀缓存命中率 |
|---|---|---|---|---|---|---|---|
| base-on-lora-server | 1 | 64 | 83.8 | 570.4 | 2251.6 | 112.4 | 71.0% |
| base-on-lora-server | 8 | 105 | 155.8 | 2519.3 | 5184.5 | 303.0 | 66.1% |
| base-on-lora-server | 32 | 453 | 325.1 | 5539.8 | 14302.8 | 500.5 | 69.1% |
| base-prefix-cache-off | 1 | 64 | 519.4 | 699.6 | 2639.6 | 100.3 | - |
| base-prefix-cache-off | 8 | 105 | 1396.1 | 2552.9 | 7869.6 | 225.9 | - |
| base-prefix-cache-off | 32 | 453 | 1503.7 | 4294.9 | 22992.9 | 321.4 | - |
| base-prefix-cache-on | 1 | 64 | 84.6 | 570.9 | 2243.8 | 112.5 | 71.1% |
| base-prefix-cache-on | 8 | 105 | 154.6 | 2829.9 | 5179.3 | 304.1 | 66.1% |
| base-prefix-cache-on | 32 | 453 | 410.2 | 5501.0 | 14156.0 | 504.1 | 69.1% |
| lora-adapter | 1 | 64 | 95.6 | 664.9 | 2945.9 | 85.6 | 70.8% |
| lora-adapter | 8 | 105 | 176.9 | 2840.6 | 5683.2 | 266.2 | 66.0% |
| lora-adapter | 32 | 453 | 330.0 | 6158.0 | 15061.6 | 463.4 | 68.9% |

## 无 verifier 的失败预测

7399 个已验证 episode（失败占 58.8%），928 个场景，按场景分组 5 折。标签是 verifier 的判定。

| 模型 | AUROC | AUPRC | 复核 10% 召回 | 复核 20% 召回 | 复核 30% 召回 |
|---|---|---|---|---|---|
| logistic_regression | 0.675 | 0.751 | 14.9% | 28.5% | 39.8% |
| gradient_boosting | 0.652 | 0.739 | 14.9% | 28.5% | 39.7% |

| 单一规则 | 标记比例 | 召回 | 精确率 |
|---|---|---|---|
| tool_errors>0 | 26.4% | 29.0% | 64.7% |
| terminated | 13.1% | 16.9% | 75.4% |
| no_write | 27.8% | 36.1% | 76.4% |
