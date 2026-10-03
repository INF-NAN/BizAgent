# 部署指南：GPU 模型服务与 smoke 训练

本文说明两件事：在 Linux GPU 机器上用 vLLM 服务 Arctic-AWM-4B，并把 workbench、`awm agent` 与 `awm verify` 接到这个服务；在独立的 train 环境里运行 smoke 训练。CPU 与 mock LLM 的快速开始见 [README](../README.md)，组件关系见 [ARCHITECTURE.md](ARCHITECTURE.md)，上游版本与源码依据见 [UPSTREAM.md](UPSTREAM.md)，文中的 ADR 编号见 [DECISIONS.md](DECISIONS.md)。

本文的命令用于部署服务和跑通链路，单次运行的结果不构成对模型或应用的评价（ADR-013）。在官方任务上批量评测并训练小模型的一键实验是另一套流程，见 [EXPERIMENTS.md](EXPERIMENTS.md)。

一台机器可以先做模型服务再做 smoke 训练：第 2 节只做一次，开始训练前停掉 vLLM。

## 1. 机器要求

| 项目 | 模型服务 | smoke 训练 | 依据 |
|---|---|---|---|
| GPU | 1 张 Ampere（sm_80）或更新架构的显卡，建议 24 GB 显存及以上 | 1 张 sm_80 或更新架构的显卡（例如 A100），至少一张卡空闲显存 ≥ 12000 MiB | Arctic-AWM-4B 的权重为 bf16，两个 safetensors 分片约 5.0 GB 与 3.8 GB；bf16 需要 sm_80 及以上。vLLM 按 `gpu_memory_utilization` 0.9 占用显存，扣除权重后的空间用作 KV cache，至少要放下一条 `max_model_len` 长度的序列（由模型 config 得出，为 40960）。`workbench train preflight` 要求空闲显存 ≥ 12000 MiB（`src/workbench/train/preflight.py`）；smoke profile 中 vLLM rollout 的 `gpu_memory_utilization` 为 0.4；flash-attn 默认只为 sm_80、90、100、120 编译 |
| 驱动 | `nvidia-smi` 显示的 `CUDA Version` ≥ 12.8 | 同左 | train 环境锁定 torch 2.10.0，它的 CUDA 运行库为 12.8（`nvidia-cuda-runtime-cu12` 12.8.90），随 wheel 安装，不依赖系统里的 CUDA toolkit，所以按驱动支持 CUDA 12.8 来选机器 |
| CUDA toolkit（`nvcc`） | 不需要 | 12.x，并设置 `CUDA_HOME` | 只有编译 flash-attn 时用到。flash-attn 2.8.3.post1 在 PyPI 上只有源码包，要求 `CUDA_HOME` 与 nvcc ≥ 11.7（flash-attn `setup.py:168-175`）；PyTorch 编译扩展时要求 nvcc 与 torch 的 CUDA 主版本相同 |
| 内存与 CPU | 无额外要求 | 按 `MAX_JOBS` × 9 GB 预留内存；核数越多编译越快 | flash-attn 的编译任务每个峰值约 9 GB（`setup.py:513-526` 的注释），并行数由 `MAX_JOBS` 控制（第 4.2 节） |
| 磁盘（数据盘） | 建议 ≥ 50 GB | 与模型服务同机时建议 ≥ 70 GB | train 环境按 `train/uv.lock` 中 linux x86_64 wheel 的大小合计下载约 5.4 GB，安装后更大；Arctic-AWM-4B 约 8.8 GB；另有 app 环境、官方数据集、uv 与 Hugging Face 缓存；smoke 训练再加 flash-attn 构建与 Qwen3-0.6B |
| 系统 | Linux x86_64，装有 `gcc` | 同左 | `train/pyproject.toml` 只为 linux x86_64 加锁；Triton 在运行时用 `gcc` 编译小段代码；flash-attn 编译需要 C++ 编译器 |
| Python | 3.12 | 同左 | 根项目与 train 项目都要求 `>=3.12,<3.13`；`workbench doctor` 对其它版本报 fail |

## 2. 公共准备

### 2.1 取代码

```bash
nvidia-smi                       # GPU 型号、显存与 CUDA Version
cd /root/autodl-tmp              # AutoDL 的数据盘；其它机器换成空间足够的目录
git clone https://github.com/IntheFesh/Agent2.git
cd Agent2
git submodule update --init third_party/agent-world-model third_party/AgentFly
git submodule status
```

`git submodule status` 输出两行，提交分别以 `85e322f` 与 `1256586` 开头，行首为空格；行首为 `-` 表示没有初始化，为 `+` 表示检出的提交与固定版本不同。AgentFly 嵌套的 `verl` 子模块只有 smoke 训练需要（第 4.1 节）。

GitHub 访问受限时：

- AutoDL 可以临时开启学术资源加速：`source /etc/network_turbo`（以 AutoDL 文档为准），它为 GitHub 与 PyPI 设置 `http_proxy`、`https_proxy`；下载结束后执行 `unset http_proxy https_proxy`。
- 或者在能访问 GitHub 的机器上克隆（需要训练时连同第 4.1 节的子模块），打包上传后解压。

### 2.2 uv 与 Python 3.12

```bash
pip install uv==0.8.17
uv python find 3.12 || uv python install 3.12
```

- 使用 uv 0.8.17：Dockerfile 用的是同一版本；`train/pyproject.toml` 用到的 `extra-build-dependencies` 在 uv 中仍是实验特性，行为可能随版本变化（ADR-025）。
- 系统 Python 拒绝 `pip install`（`externally-managed-environment`）时，改用 `pipx install uv==0.8.17`。
- `uv python install` 从 GitHub 下载解释器。下载失败时可以用 conda：`conda create -y -p /root/autodl-tmp/py312 python=3.12`，再在下一节的 `env.sh` 里加 `export UV_PYTHON=/root/autodl-tmp/py312/bin/python3.12`。

### 2.3 环境变量

在仓库根目录生成 `data/gpu/env.sh`（`data/` 被 git 忽略），之后每开一个终端都先 `source data/gpu/env.sh`，它会切换到仓库根目录。

```bash
mkdir -p data/gpu
cat > data/gpu/env.sh <<'EOF'
export REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export WORK=$(dirname "$REPO")                    # 仓库所在目录（数据盘）
export PYTHONPYCACHEPREFIX=$REPO/.cache/pycache
export UV_CACHE_DIR=$WORK/.cache/uv
export UV_HTTP_TIMEOUT=300
export UV_NO_SYNC=1
export HF_HOME=$WORK/.cache/huggingface
export HF_ENDPOINT=https://hf-mirror.com          # 能直连 huggingface.co 时删掉这一行
export HF_HUB_DISABLE_XET=1
export VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
cd "$REPO"
EOF
source data/gpu/env.sh
```

| 变量 | 作用 |
|---|---|
| `PYTHONPYCACHEPREFIX` | 字节码写到 `.cache/pycache`，运行 AWM 与 AgentFly 时不在 `third_party/` 的子模块里留下 `__pycache__`（Makefile 也这样设置） |
| `UV_CACHE_DIR`、`HF_HOME` | uv 缓存与 Hugging Face 缓存放在数据盘。uv 缓存与环境在同一文件系统时，安装以硬链接完成，不重复占用空间 |
| `UV_HTTP_TIMEOUT` | 网络慢时下载大 wheel 不因超时失败 |
| `UV_NO_SYNC=1` | `uv run` 默认先同步环境。`scripts/serve_vllm.sh` 以 `uv run --project train vllm serve …` 启动服务，同步会安装完整的 train 环境，从而开始编译 flash-attn；模型服务只安装不含 flash-attn 的部分（第 3.1 节）。workbench 自己启动训练进程时已显式带 `--no-sync` |
| `HF_ENDPOINT` | 中国大陆访问 huggingface.co 受限时改用镜像站 `hf-mirror.com` |
| `HF_HUB_DISABLE_XET=1` | app 环境的 huggingface_hub 1.32.0 装有 `hf_xet`，Xet 协议的下载不经过 `HF_ENDPOINT`；关闭后所有文件都经 `HF_ENDPOINT` 下载（`huggingface_hub/constants.py:342`、`utils/_runtime.py:155-157`） |
| `VLLM_NO_USAGE_STATS`、`DO_NOT_TRACK` | vLLM 默认上报使用统计，任一变量为 1 即关闭（`vllm/usage/usage_lib.py:52-66`）。训练进程的白名单按 `VLLM_` 前缀放行前者，不放行 `DO_NOT_TRACK`（第 4.5 节），所以两个都设 |
| `NO_PROXY`、`no_proxy` | 开了代理时，httpx 与 OpenAI SDK 会把发往本机 vLLM 和环境服务的请求也交给代理；这里把本机地址排除在外 |

### 2.4 应用环境

```bash
uv sync --frozen
```

`uv sync --frozen` 严格按 `uv.lock` 记录的地址（files.pythonhosted.org）下载，`UV_DEFAULT_INDEX` 等 PyPI 镜像设置对它不起作用；改用 `--locked` 时 uv 会从镜像重新解析，而锁文件记录的 registry 是 pypi.org，于是报 `lockfile needs to be updated`。不要为了换镜像重新执行 `uv lock`，那样得到的环境与仓库锁定的版本不同。下载慢时开启 AutoDL 学术资源加速或配置 HTTP 代理。

### 2.5 官方数据集

```bash
AWM1K_REVISION=dde80a0283fe781bdc51656bce57063dc5650213 ./scripts/download_data.sh data/awm1k
```

`make data` 调用同一个脚本；不设 `AWM1K_REVISION` 时，脚本先向 Hugging Face 查询数据集当前的提交，再按这个提交下载。测试与文档对应 revision `dde80a0`。下载结束后脚本写出 `data/awm1k/MANIFEST.json`，记录来源、revision 与许可证。

数据集 AgentWorldModel-1K 的许可证为 CC-BY-4.0，使用时须署名（[UPSTREAM.md](UPSTREAM.md) §5）。它只下载到本地，不入库。

### 2.6 自检

```bash
uv run workbench doctor
```

检查项依次为 Python 3.12、两个子模块是否在固定提交、数据集文件、端口（API 8080、网关 8081、环境端口池 18100–18199）、`nvidia-smi`、环境变量与 LLM 后端。有 fail 时退出码为 1；warn 只是提示。

## 3. 模型服务

### 3.1 train 环境中的推理部分

vLLM 装在 train 环境里，app 环境不安装 torch 与 vLLM（ADR-002）。只做模型服务时不装 flash-attn：

```bash
(cd train && uv sync --frozen --no-install-package flash-attn)
uv run --project train python -c "import torch, vllm; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), vllm.__version__)"
```

- 第二条命令打印 torch 与 vLLM 的版本以及 CUDA 是否可用。锁文件中的版本是 torch 2.10.0 与 vLLM 0.19.0；`torch.cuda.is_available()` 为 False 时先检查驱动。
- vLLM 在 CUDA 上使用自带的 `vllm.vllm_flash_attn`，外部的 flash-attn 包只在 ROCm 上使用（`vllm/v1/attention/backends/fa_utils.py:18-44`）。flash-attn 只能在本机编译，所以留到 smoke 训练再装。
- AgentFly 以 editable 方式安装，产生的 `*.egg-info`、`build/` 都在 AgentFly 自己的 `.gitignore` 里，`git -C third_party/AgentFly status --porcelain` 仍为空。

### 3.2 下载模型

```bash
uv run hf download Snowflake/Arctic-AWM-4B --revision 437dfa0e12702901eb41c30e9326a90996549650 --local-dir "$WORK/models/Arctic-AWM-4B"
md5sum "$WORK/models/Arctic-AWM-4B/chat_template.jinja"
```

- 这个 revision 的 `chat_template.jinja` 的 md5 为 `da05f6b8a81932c7cf5f26eb545d4417`；目录中还有 `config.json`、两个 safetensors 分片、`model.safetensors.index.json` 与 tokenizer 文件。
- 模型许可证为 Apache-2.0。权重不入库。

### 3.3 启动 vLLM

启动参数全部来自 serving profile `configs/serving/arctic-awm-4b.yaml`。用本地目录时，复制一份 profile，只把 `model` 换成本地路径；`served_model_name` 保持 `Snowflake/Arctic-AWM-4B`，workbench 与 `awm agent` 都按这个名字请求模型。

```bash
sed "s#^model: Snowflake/Arctic-AWM-4B\$#model: $WORK/models/Arctic-AWM-4B#" configs/serving/arctic-awm-4b.yaml > data/gpu/arctic-awm-4b.local.yaml
uv run workbench serve vllm-cmd --profile data/gpu/arctic-awm-4b.local.yaml
PROFILE=data/gpu/arctic-awm-4b.local.yaml scripts/serve_vllm.sh 2>&1 | tee data/gpu/vllm-serve.log
```

- `workbench serve vllm-cmd` 打印由 profile 生成的完整 `vllm serve` 命令，可以先核对。`scripts/serve_vllm.sh` 用 `--args-only` 取得同样的参数，在前台以 `uv run --project train` 启动 vLLM，所以放在单独的终端里运行。
- profile 各项：`host` 127.0.0.1、`port` 8000；`max_model_len` 为 null，由 vLLM 从模型 config 推导；`gpu_memory_utilization` 0.9；`enable_auto_tool_choice: true` 与 `tool_call_parser: hermes`；`reasoning_parser` 与 `chat_template` 为 null，使用模型自带的模板。
- 服务就绪后，`curl -s http://127.0.0.1:8000/v1/models` 列出的模型名就是 `served_model_name`。

`--enable-auto-tool-choice --tool-call-parser hermes` 的作用（ADR-018，源码依据见 [UPSTREAM.md](UPSTREAM.md) §9）：

- workbench 智能体的 act 请求通过原生 `tools` 参数传工具定义。vLLM 在没有这两个参数时拒绝带 `tools` 的请求，返回 HTTP 400。
- 模型的 chat template 要求模型把工具调用写成 `<tool_call>\n{"name": …, "arguments": …}\n</tool_call>`，`hermes` parser 解析的正是这种格式，把它转换成原生 `tool_calls`。
- 不带 `tools` 的请求 `tool_choice` 保持 `"none"`，parser 不参与，`<tool_call>` 文本原样留在 `content` 中。`awm agent` 的请求属于这一类，它用自己的文本协议解析 `content`。
- 没有设置 reasoning parser：`<think>…</think>` 留在 `content` 中，由 workbench 客户端去掉。

### 3.4 探针

```bash
uv run workbench serve probe
```

`workbench serve probe`（`src/workbench/llm/probe.py`）先请求 `/models`，确认 `llm.model` 在服务列表中；服务连不上或没有这个模型时打印原因并以退出码 1 结束。之后各发 1 个请求：

- `native`：workbench 智能体 act 时的请求，由 `vllm` 后端构造并发送，带原生 `tools`、流式、开启思考。
- `text`：`awm agent` 的第一个请求，直接调用 AWM 的 `generate_response`，使用 AWM 的 system prompt 与它为本地 vLLM 附加的参数，不带 `tools`。

两个请求都保存服务返回的原始响应，所以能区分服务给出的内容与客户端解析出的结果。可用 `--base-url`、`--model` 覆盖配置，用 `--only native` 或 `--only text` 只发其中一个。

| 探针 | `verdict` | 含义 |
|---|---|---|
| `native` | `native` | 原始响应中有原生 `tool_calls` |
| `native` | `text-fallback` | 原始响应没有原生 `tool_calls`，后端从 `content` 里的 `<tool_call>` 文本解析出了调用 |
| `native` | `no-call` | 两者都没有，模型没有调用工具 |
| `text` | `ok` | 原始响应没有原生 `tool_calls`，AWM 的解析器从 `content` 中读出了调用 |
| `text` | `parser-interfered` | 不带 `tools` 的请求却得到了原生 `tool_calls`，parser 把调用从文本中取走，AWM 读不到 |
| `text` | `no-call` | 两者都没有，模型没有按文本协议输出调用 |
| 两者 | `error` | 请求失败，`error` 字段给出异常类型与信息，例如服务启动时缺少 tool parser 参数得到的 HTTP 400 |

任一探针为 `error` 或 `parser-interfered` 时退出码为 1，否则为 0。每个探针还报告 `native_tool_calls`、`client_tool_calls` 或 `awm_parsed_tool_calls`、`finish_reason`、`usage`、`seconds`，以及 `think_open`、`think_close`、`tool_call_text_in_content`、`content_head`、`content_tail`，用来判断 `<tool_call>` 是否写在了思考内容里。

### 3.5 把 workbench 接到 vLLM

```bash
export WORKBENCH_LLM__BACKEND=vllm
uv run workbench doctor
uv run workbench api serve          # 浏览器打开 http://127.0.0.1:8080/ui/
```

也可以在命令行跑一次智能体：

```bash
WORKBENCH_LLM__BACKEND=vllm uv run workbench agent run --scenario e_commerce_33 \
  "Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under \$200 to my cart in quantity 1."
```

- `llm.base_url` 默认 `http://127.0.0.1:8000/v1`，`llm.model` 默认 `Snowflake/Arctic-AWM-4B`，与 profile 一致，所以只需切换后端。`vllm` 后端在请求中带 `chat_template_kwargs.enable_thinking`。vLLM 启动时没有 `--api-key`，不校验 key，不需要设置 `OPENAI_API_KEY`。
- 设为 `vllm` 后端后，`workbench doctor` 的 `llm` 一项检查 `<base_url>/models` 能否访问。
- `agent run` 默认 `--approve prompt`，每个写操作执行前在终端询问；`--approve auto` 或 `deny` 用于无人值守。
- 两层超时（ADR-008）：`llm.total_timeout_s` 是单次 LLM 调用含重试的墙钟上限，默认 180 秒；`agent.wall_clock_s` 是整次运行的上限，默认 300 秒。本地单卡服务开启思考、单次最多生成 `llm.max_tokens`（默认 8192）个 token，生成速度取决于显卡，可以按需放宽，例如 `WORKBENCH_LLM__TOTAL_TIMEOUT_S=600 WORKBENCH_AGENT__WALL_CLOCK_S=1200`。这只是工程时限，不改变模型与提示。

### 3.6 docker compose

```bash
WORKBENCH_LLM__BACKEND=vllm docker compose --profile gpu up
```

- 启动 `env-manager`、`app`、`vllm` 三个服务。`vllm` 使用镜像 `vllm/vllm-openai:v0.19.0`，参数与 `workbench serve vllm-cmd` 的输出相同，只有 `--host` 为 `0.0.0.0`，供其它容器访问；`app` 的 `WORKBENCH_LLM__BASE_URL` 默认指向 `http://vllm:8000/v1`。
- GPU 通过 compose 的设备预留分配，宿主机需要 NVIDIA Container Toolkit。
- `vllm` 服务的命令写在 `docker-compose.yml` 中，不读 serving profile；单元测试 `test_compose_vllm_matches_the_serving_profile` 保证两者一致。
- `vllm` 服务按模型 id 加载，没有固定 revision；缓存目录挂载的是宿主机的 `${HOME}/.cache/huggingface`，而不是 `HF_HOME`，容器内也没有设置 `HF_ENDPOINT`。容器需要能访问 huggingface.co，或者事先把模型放进这个缓存目录。
- `app` 与 `env-manager` 把 `./data` 挂载为 `/data`，从 `/data/awm1k` 读取数据集，所以先完成第 2.5 节。
- 这两个服务的镜像由 `Dockerfile` 构建，其中复制了 `third_party/agent-world-model`，内含 AWM 代码。AWM 仓库没有许可证，镜像只在本地和 CI 中构建，不得推送到任何公开或私有的镜像仓库（ADR-003）。

### 3.7 `awm agent`

`awm agent` 以 `--mcp_url` 模式连接由 env-manager 启动的会话。env-manager 启动 AWM server 时显式传 `--db_path`、`--temp_server_path`、`--output_dir`，全部落在会话目录内，结束时按进程组回收（ADR-004）。

```bash
WORKBENCH_ENV__RUNS_DIR=data/gpu/awm-runs WORKBENCH_ENV__IDLE_TIMEOUT_S=3600 \
  uv run workbench env serve > data/gpu/env-serve.log 2>&1 &
ENV_PID=$!
uv run workbench env up e_commerce_33 --session-id awm0
```

`env up` 打印会话的 JSON，其中 `url` 形如 `http://127.0.0.1:18100/mcp`；下面的命令换成实际地址。

```bash
uv run awm agent --scenario e_commerce_33 --task_id 0 --tasks_path data/awm1k/gen_tasks.jsonl \
  --mcp_url http://127.0.0.1:18100/mcp --api_url http://localhost:8000/v1 --model Snowflake/Arctic-AWM-4B \
  --output_dir data/gpu/awm-agent/e_commerce_33_task_0
uv run workbench env diff awm0
uv run workbench env down awm0
kill "$ENV_PID"
```

- 不用 `--scenario` 自动起服，因为上游在这条路径上有三个问题：工作库建成 `<output_dir>/final.db` 后结束时又复制到同一路径，必然抛 `shutil.SameFileError`；服务代码写到 `--envs_path` 所在目录，即官方数据目录；结束时只终止启动器，服务进程残留（[UPSTREAM.md](UPSTREAM.md) §6.4）。
- 同时给 `--scenario`、`--task_id` 与 `--mcp_url` 时，任务文本从 `--tasks_path` 查出并写进 `trajectory.json`，AWM 不起服也不建库。`--tasks_path` 的默认值在 `./outputs/` 下，所以要显式指向数据集。
- `--api_url` 必须同时含 `localhost` 与 `v1`，不能写成 `127.0.0.1`：AWM 只在这种地址下附加 vLLM 参数（`add_generation_prompt`、`min_tokens` 16、`enable_thinking`），并以 `tool` 角色回传工具结果；否则工具结果以 user 消息回传（`awm/core/agent.py:355-379,417`）。
- 不需要设置 `OPENAI_API_KEY`：AWM 在没有它时使用占位值 `EMPTY`（`awm/tools.py:429`），本机 vLLM 不校验 key。机器上不设置任何 key，进程里也就没有 key 可泄露。
- 其余参数为上游默认：`max_iterations` 30、`temperature` 1.0、`max_tokens` 2048。思考内容也计入 `max_tokens`；某一轮输出中没有 `<tool_call>` 时，AWM 把这段输出当作最终回答并结束循环（`awm/core/agent.py:506-518`）。
- `awm agent` 直接访问 MCP 地址，不经过 env-manager，会话的最近使用时间不会更新；到了 `env.idle_timeout_s`（默认 600 秒）会话就会被回收。上面启动 `env serve` 时把它放宽到 3600 秒。
- `workbench env diff` 对比会话数据库与初始库，列出各表的行数与主键变化。

### 3.8 `workbench verify`

```bash
uv run workbench verify --input data/gpu/awm-agent/e_commerce_33_task_0 --mode code \
  --init-db data/gpu/awm-runs/awm0/initial.db --final-db data/gpu/awm-runs/awm0/work.db
```

- `workbench verify` 包装 `awm verify`（ADR-023）。code 模式下子进程只拿到白名单中的环境变量（ADR-017），没有任何 key；verifier 默认取 `data/awm1k/gen_verifier.pure_code.jsonl`。
- code 模式不需要裁判：只有 sql 模式调用 LLM 裁判，code 模式执行数据集自带的纯代码 verifier，结果为 `complete` 或 `others`（[UPSTREAM.md](UPSTREAM.md) §6.5）。
- `--mcp_url` 模式的输出目录里没有数据库，所以要显式传会话的初始库 `initial.db` 与工作库 `work.db`。`env down` 只停止服务，不删除会话目录。
- 命令打印 `mode`、`verifier`、`log`、`output` 与 `reward_type`；`awm verify` 的结果写在输出目录的 `verify.code.json`，日志在 `verify.code.log`。

### 3.9 停止服务

在 vLLM 所在的终端按 Ctrl-C，再用 `nvidia-smi` 确认显存已经释放。同机接着做 smoke 训练时必须先停掉 vLLM，否则 preflight 的显存检查不通过。

子模块应保持干净：

```bash
git -C third_party/agent-world-model status --porcelain
git -C third_party/AgentFly status --porcelain
```

两条命令都应没有输出。

## 4. smoke 训练

smoke profile `configs/train/smoke.yaml` 只演示一次"rollout → 奖励 → 更新"循环：Qwen3-0.6B、LoRA（rank 8）、GRPO、3 个 step、batch 2，使用 AgentFly 内置的 `calculator` 工具与 `math_equal_reward_tool`，数据是 `configs/train/smoke_data.json` 中 8 道手写的算术题（ADR-012）。`TrainProfile.validate` 只接受名为 `smoke`、模型不超过 1.7B、启用 LoRA、step 数不超过 5、标记 `no_results: true` 的 profile。

新终端先 `source data/gpu/env.sh`；新机器先完成第 2 节。

### 4.1 veRL 嵌套子模块

```bash
git -C third_party/AgentFly -c url."https://github.com/".insteadOf="git@github.com:" submodule update --init verl
git -C third_party/AgentFly submodule status verl
```

- AgentFly 的 `.gitmodules` 用 SSH 地址 `git@github.com:Agent-One-Lab/verl.git`。`-c url…insteadOf` 只让这一次克隆改走 HTTPS，经 `GIT_CONFIG_PARAMETERS` 传给 `git submodule` 启动的克隆进程，不修改 `.gitmodules`。
- AgentFly @`1256586` 固定的 veRL 提交为 `001f000`，版本文件 `verl/verl/version/version` 为 `0.8.0.dev`；`submodule status` 的行首应为空格。
- AgentFly 与 veRL 的许可证都是 Apache-2.0。安装方式与配置键见 [UPSTREAM.md](UPSTREAM.md) §7。

### 4.2 完整的 train 环境

```bash
nvcc --version
cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes
export CUDA_HOME=/usr/local/cuda
export FLASH_ATTN_CUDA_ARCHS=80
export FLASH_ATTENTION_FORCE_BUILD=TRUE
export MAX_JOBS=6
export NVCC_THREADS=4
(cd train && uv sync --frozen)
uv run --project train python -c "import torch, flash_attn; from flash_attn import flash_attn_func; print(torch.__version__, flash_attn.__version__)"
```

| 变量 | 作用 |
|---|---|
| `CUDA_HOME` | flash-attn 用它找到 nvcc（`setup.py:168-175`）。nvcc 不在 `/usr/local/cuda` 时，设为 `which nvcc` 的上两级目录 |
| `FLASH_ATTN_CUDA_ARCHS` | 默认 `80;90;100;120`（`setup.py:70`）。只为本机显卡的架构编译可以缩短编译时间：A100、A800 为 80，H100、H800 为 90 |
| `FLASH_ATTENTION_FORCE_BUILD=TRUE` | `setup.py` 默认先按构建环境中 torch 的版本拼出 GitHub 上预编译 wheel 的地址并尝试下载（`:440-472`）；设置后直接编译（`:61`、`:484-486`） |
| `MAX_JOBS` | 并行编译任务数。未设置时 flash-attn 按 `min(CPU 核数/2, psutil 读到的可用内存 GB/9)` 计算（`setup.py:513-526`），而容器里 psutil 读到的是宿主机内存，不是容器的内存上限，编译可能因内存不足被 kill。按第二条命令查到的容器内存上限计算 `min(CPU 核数/2, 容器内存 GB/9)`；输出 `max` 表示没有 cgroup 限制，按租用页面标明的内存计算 |
| `NVCC_THREADS` | 每个编译任务内 nvcc 的线程数，默认 4（`setup.py:124`） |

- 第一条 `uv sync` 会在本机编译 flash-attn 2.8.3.post1，耗时取决于 CPU 核数与 `MAX_JOBS`；最后一条确认 `flash_attn_func` 能够导入。
- `train/pyproject.toml` 把 flash-attn 构建环境里的 torch 固定为锁定版本：`flash-attn = [{ requirement = "torch", match-runtime = true }]`（ADR-025）。写成 `"torch"` 时，uv 会在构建环境里安装 PyPI 上最新的 torch，编出的扩展与运行时的 torch 2.10.0 不兼容。uv 只读取项目根目录的 `[tool.uv]`，所以不需要修改 AgentFly（[UPSTREAM.md](UPSTREAM.md) §8）。

### 4.3 下载 Qwen3-0.6B

smoke profile 以 Hugging Face id `Qwen/Qwen3-0.6B` 引用模型。先下载到 `$HF_HOME` 的缓存，训练时用 `HF_HUB_OFFLINE=1` 只读缓存。

```bash
uv run hf download Qwen/Qwen3-0.6B
ls "$HF_HOME/hub/models--Qwen--Qwen3-0.6B/snapshots/"
```

- 下载时不要加 `--revision <提交哈希>`：huggingface_hub 只在 revision 不是提交哈希时才写入 `refs/<revision>`（`huggingface_hub/file_download.py:723-728`），离线模式下按 id 加载要通过 `refs/main` 找到快照。
- 快照目录名就是所用的提交，需要复现时记下它。
- 训练时的 `HF_HOME` 必须与下载时相同。`HF_` 前缀的变量会传给训练进程（第 4.5 节）。

### 4.4 preflight

```bash
HF_HUB_OFFLINE=1 uv run workbench train preflight
```

| 检查 | 内容 |
|---|---|
| `profile` | 加载并校验 smoke profile，显示模型、参数量、LoRA、step 数与 `NO_RESULTS` |
| `gpu` | `nvidia-smi` 列出的显卡中至少一张空闲显存 ≥ 12000 MiB |
| `train-env` | 在 train 环境里 `import torch, vllm`，报告版本，并要求 CUDA 可用 |
| `verl` | 嵌套的 `verl` 子模块已初始化，报告其版本 |
| `config-keys` | 静态检查：profile 的每个覆盖键，以及 launch 另外设置的数据路径、模型路径、实验名与输出目录，都存在于 veRL 的 `ppo_trainer.yaml` 或 `_generated_ppo_trainer.yaml` 中；读取时忽略 fork 中残留的合并冲突标记行 |
| `hydra-compose` | 在 train 环境里让 Hydra 用全部覆盖项组合 `ppo_trainer` 配置，这是配置键的权威检查 |
| `data` | 训练与验证文件存在，每行都有 `question` 与 `answer` |

所有探测都在训练将要使用的环境里运行（`train_env`，ADR-024），所以 preflight 检查的就是训练会看到的环境。有 fail 时退出码为 1。

### 4.5 查看将要执行的命令

```bash
HF_HUB_OFFLINE=1 uv run workbench train launch
```

不带 `--execute` 时只打印计划，是一个 `"mode": "dry-run"` 的 JSON：

- `command`：`uv run --project train --no-sync python -m agentfly.cli train <Hydra 覆盖项>`，其中数据、模型、实验名与输出目录由 launch 填入；
- `env.passed` 与 `env.withheld`：训练进程拿到的变量名与被拦下的变量名，只有名字，没有值；
- `preflight`：第 4.4 节各项检查的结果。

训练进程的环境是白名单（`src/workbench/subprocess_env.py` 的 `train_env`，ADR-024）：

- 放行 `PATH`、locale、时区、临时目录、Python I/O 设置与 `PYTHONPYCACHEPREFIX`；代理与 CA 设置（包括 `NO_PROXY`）；`HOME`、`LD_LIBRARY_PATH`、`CC`、`CXX`、线程数与确定性开关、`UV_CACHE_DIR` 等 uv 与 AgentFly 的设置；
- 放行 `CUDA_`、`NVIDIA_`、`NCCL_`、`GLOO_`、`TORCH_`、`PYTORCH_`、`TORCHINDUCTOR_`、`TRITON_`、`VLLM_`、`RAY_`、`VERL_`、`HF_`、`HUGGINGFACE_`、`TRANSFORMERS_`、`AGENTFLY_` 前缀下名字不含 KEY、TOKEN、SECRET、PASSW、CREDENTIAL、AUTH 的变量；
- 其它变量一律不传，包括 `HF_TOKEN`、`WANDB_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 以及云与 git 的凭据。

机器确实需要某个被拦下的变量时，按名字显式放行：`WORKBENCH_TRAIN__ENV_PASSTHROUGH='["变量名"]'`，或写进 `configs/app.yaml` 的 `train.env_passthrough`。smoke 训练从本地缓存离线加载公开模型，不需要 `HF_TOKEN`。

### 4.6 执行 smoke 训练

```bash
HF_HUB_OFFLINE=1 uv run workbench train launch --execute
```

- `--execute` 要求 preflight 全部通过，否则打印 `preflight failed: [...]` 并以退出码 1 结束。
- 运行目录为 `data/train_runs/<YYYYmmdd_HHMMSS>_smoke/`，包含：
  - `NO_RESULTS`：说明该目录来自 smoke profile，只演示循环，不得用于任何效果结论，与 Arctic-AWM 及论文结果无关；
  - `command.json`：实际执行的命令；
  - `env_names.json`：放行与拦下的变量名；
  - `train.log`：训练进程的全部 stdout 与 stderr。
- 训练输出只写入 `train.log`；命令结束时 workbench 打印 `{"mode": "execute", "run_dir": "…", "returncode": …}`。
- veRL 的 console logger 每个 step 在 `train.log` 中写一行 `step:N - key:value - …`（`verl/utils/logger/aggregate_logger.py:26-31`）。出现这些行说明循环跑到了对应的 step；其中的 reward、score 等指标只是日志，不作为结果解读（ADR-013）。

### 4.7 记录显存峰值（可选）

```bash
nvidia-smi --query-gpu=timestamp,memory.used,memory.total --format=csv,noheader -l 1 > data/gpu/gpu-mem.csv &
MON_PID=$!
HF_HUB_OFFLINE=1 uv run workbench train launch --execute
kill "$MON_PID"
awk -F', ' '{gsub(/ MiB/, "", $2); if ($2 + 0 > m) m = $2 + 0} END {print "peak memory.used:", m, "MiB"}' data/gpu/gpu-mem.csv
```

这里记录的是整卡已用显存，包含 vLLM rollout 按 `gpu_memory_utilization` 0.4 预留的部分。

## 5. 分享日志前脱敏

```bash
uv run python scripts/redact_log.py data/gpu/vllm-serve.log
```

脚本在输入文件旁写出 `<名字>.redacted<扩展名>`（上例为 `data/gpu/vllm-serve.redacted.log`），打印各类替换的次数，不改动输入文件。

替换的内容：

- 以 `hf_`、`sk-`、`ghp_`、`gho_`、`ghs_`、`github_pat_`、`xox[abprs]-` 开头的 token → `[TOKEN]`；
- URL 中的用户名与密码 → `[CREDENTIALS]`；
- `Bearer` 之后的值 → `[TOKEN]`；
- 名字含 KEY、TOKEN、SECRET、PASSWORD、PASSWD、CREDENTIAL 的赋值 `NAME=value` 中的值 → `NAME=[REDACTED]`；
- 邮箱 → `[EMAIL]`；
- 13–19 位、可用空格或连字符分组的卡号 → `[CARD ****末四位]`；
- 电话 → `[PHONE]`。电话按数据集的写法识别：带 `-`、空格、括号或 `+` 的号码（例如 `555-111-2222`、`+1-312-555-0100`、`+12125550123`）以及大陆手机号，匹配不跨行。

邮箱、卡号与电话的规则沿用网关审计日志的脱敏规则（`workbench.gateway.audit`，ADR-014）。以下内容保留，因为它们只是看起来像电话或密钥：

- 版本号与 IPv4 地址，例如驱动版本；
- 小数；
- 按列对齐的表格数字；
- 单独的整数，例如文件大小与计数；
- `YYYY-MM-DD` 日期与时间戳；
- 值只是变量名的赋值，例如 `API_KEY_ENV=DEEPSEEK_API_KEY`；
- `git@github.com:…` 这类 SSH 地址。

规则识别不了所有秘密，也不识别姓名与地址。分享前列出剩下的可疑行，逐行确认：

```bash
grep -nEi 'api[_-]?key|token|secret|passw|bearer|@' data/gpu/vllm-serve.redacted.log
```

只剩变量名（例如 `env_names.json` 里的 `HF_TOKEN`）没有问题；看到真实的值就手动改成 `[REDACTED]`。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `uv sync` 下载很慢或超时 | 调大 `UV_HTTP_TIMEOUT`，开启 AutoDL 学术资源加速或 HTTP 代理后重试；不要为换镜像重新 `uv lock`（第 2.4 节） |
| vLLM 启动失败，报 KV cache 放不下 `max_model_len`（40960）长度的序列 | 显存扣除权重后不够一条最长序列。确认没有其它进程占用显存，或换显存更大的卡。也可以在本地 profile 副本里设较小的 `max_model_len`，代价是长轨迹（39 个工具的清单加多轮工具结果）可能超出上下文 |
| vLLM 或 Triton 报 `Python.h`、`gcc` 相关的编译错误 | 安装 `gcc`；使用 uv 下载的 Python 3.12 或 conda 的 Python，两者都带头文件 |
| 探针的 `native.verdict` 为 `error`，信息含 HTTP 400 | 服务启动时没有带 `--enable-auto-tool-choice --tool-call-parser hermes`。用 `workbench serve vllm-cmd --profile …` 核对启动参数，确认启动时用的是同一个 profile |
| 探针、`workbench doctor` 或 `awm agent` 访问本机服务超时，机器上开了代理 | 检查 `NO_PROXY` 与 `no_proxy` 是否包含 `localhost,127.0.0.1,::1`（第 2.3 节） |
| `workbench env up` 报 `no free port in 18100-18199` 或 `no env capacity within …` | `uv run workbench env ls` 查看残留的会话，`uv run workbench env down <会话 id>` 后重试；`workbench doctor` 的 `ports` 一项显示端口池中被占用的数量 |
| `workbench env serve` 报 8090 端口已被占用 | 上一个 `env serve` 还在运行，结束它后重试 |
| `awm agent` 运行中途连不上 MCP 地址 | 会话因空闲超时被回收。启动 `env serve` 时调大 `WORKBENCH_ENV__IDLE_TIMEOUT_S`（第 3.7 节） |
| `workbench verify` 报 `has no trajectory.json` 或 `verifier file not found` | `--input` 要指向 `awm agent` 的输出目录；默认 verifier 在 `data/awm1k/` 下，先下载数据集 |
| flash-attn 编译进程被 kill（`Killed` 或 `signal 9`） | 调小 `MAX_JOBS`（例如 2）后重新执行 `(cd train && uv sync --frozen)` |
| preflight 的 `gpu` 为 fail | 显存被模型服务的 vLLM 占用。停掉它，用 `nvidia-smi` 确认显存释放后重试 |
| preflight 的 `train-env` 为 fail | train 环境没有安装完整或 CUDA 不可用。重新执行第 4.2 节，检查驱动 |
| preflight 的 `verl`、`config-keys`、`hydra-compose` 为 fail，提示 verl 子模块没有初始化 | 执行第 4.1 节 |
| 训练报找不到 `Qwen/Qwen3-0.6B` | 确认下载时没有加 `--revision`，并且训练时的 `HF_HOME` 与下载时相同 |
