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
