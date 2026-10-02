#!/usr/bin/env bash
# The whole lab run (docs/EXPERIMENTS.md), unattended, on one Linux machine with one CUDA GPU:
#   DEEPSEEK_API_KEY=... nohup bash scripts/lab/run_all.sh > data/lab/run.log 2>&1 &
# Every stage writes data/lab/.stages/<stage>.done when it finishes and is skipped next time, and
# every evaluation resumes from its results.jsonl, so after an interruption the same command
# continues where it stopped. Per-stage logs: data/lab/logs/<stage>.log.
#
# Settings (environment variables, all optional except DEEPSEEK_API_KEY):
#   BASE_MODEL=Qwen/Qwen3-4B-Instruct-2507  MAX_MODEL_LEN=65536
#   TEACHER_MODEL=deepseek-flash  TEACHER_BASE_URL=https://api.deepseek.com
#   TEACHER_MIN_SUCCESS=600 (fewer solved train tasks: the teacher also runs the reserve train tasks)
#   TEACHER_TEST_LIMIT=300 (the teacher's reference run on test)
#   CONC=32 (episodes at once against vLLM)  TEACHER_CONC=16  AWM1K_REVISION=dde80a0283fe...
#   TEACHER_TRAIN_LIMIT=<n> (teacher on the first n of the 1500 train tasks only, to spend less)
#   SKIP_SETUP=1 (environments, dataset and model already in place)
#   PREPARE_ONLY=1 (only install the environments and download dataset and model; no GPU or key
#     needed, so it can run on a CPU-only machine first)
#   SHUTDOWN_WHEN_DONE=1 (power the machine off when the run ends, finished or failed; results are
#     packed to <lab dir name>_results.tgz first)
#   MIN_SFT_EPISODES=20 (a training-data variant with fewer episodes is skipped)
#   LAB_DIR=data/lab (where everything of this run goes; a trial run uses another one)
#   SPLIT_ARGS="--test-tasks 12 --val-tasks 6 --train-tasks 16 --inject-tasks 8" (a small trial run)
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
LAB="${LAB_DIR:-data/lab}"
[[ "$LAB" = /* ]] || LAB="$ROOT/$LAB"
export WORKBENCH_LAB_DIR="$LAB"  # every `workbench lab` command below uses it
STAGES="$LAB/.stages"
LOGS="$LAB/logs"
mkdir -p "$STAGES" "$LOGS"

BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B-Instruct-2507}"
TEACHER_MODEL="${TEACHER_MODEL:-deepseek-flash}"
TEACHER_BASE_URL="${TEACHER_BASE_URL:-https://api.deepseek.com}"
CONC="${CONC:-32}"
TEACHER_CONC="${TEACHER_CONC:-16}"
export AWM1K_REVISION="${AWM1K_REVISION:-dde80a0283fe781bdc51656bce57063dc5650213}"
GPU_VENV="${GPU_VENV:-$ROOT/data/lab/.venv-gpu}"  # shared by trial and full runs
GPY="$GPU_VENV/bin/python"
MODEL_DIR="$ROOT/data/models/$(basename "$BASE_MODEL")"
SERVED=base
PORT=8000
VLLM_URL="http://127.0.0.1:$PORT/v1"
VARIANTS=(teacher rft unfiltered)
MIN_SFT_EPISODES="${MIN_SFT_EPISODES:-20}"
TEACHER_MIN_SUCCESS="${TEACHER_MIN_SUCCESS:-600}"
TEACHER_TEST_LIMIT="${TEACHER_TEST_LIMIT:-300}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
PREPARE_ONLY="${PREPARE_ONLY:-0}"

# AutoDL: its academic proxy for GitHub / Hugging Face, and a Hugging Face mirror
if [[ -f /etc/network_turbo ]]; then
  # shellcheck disable=SC1091
  source /etc/network_turbo || true
  export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
fi
export HF_HUB_DISABLE_XET=1
export HF_HOME="${HF_HOME:-$ROOT/data/hf}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/data/uv-cache}"
export UV_LINK_MODE=copy
# keep bytecode out of the upstream submodules (ADR-001)
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$ROOT/.cache/pycache}"
export VLLM_LOGGING_LEVEL="${VLLM_LOGGING_LEVEL:-WARNING}"

log() { echo "[$(date '+%F %T')] $*"; }
die() { log "FAILED: $*"; exit 1; }

# stage <name> <command...>: run once; output to logs/<name>.log
stage() {
  local name="$1"; shift
  if [[ -f "$STAGES/$name.done" ]]; then log "skip $name (done)"; return 0; fi
  log "start $name"
  local t0=$SECONDS rc=0
  # in its own process group and waited for, so a stop signal reaches the script at once
  # (bash runs traps only between commands, and `wait` is interruptible) and cleanup can
  # pass it on to the whole stage
  set -m
  "$@" >> "$LOGS/$name.log" 2>&1 &
  STAGE_PID=$!
  set +m
  wait "$STAGE_PID" || rc=$?
  STAGE_PID=""
  if (( rc == 0 )); then
    touch "$STAGES/$name.done"
    log "done  $name ($(( (SECONDS - t0) / 60 )) min)"
  else
    log "error in $name, last lines of $LOGS/$name.log:"
    tail -n 40 "$LOGS/$name.log" || true
    die "$name"
  fi
}

VLLM_PID=""
TEACHER_PID=""
STAGE_PID=""
stop_vllm() {
  stop_group "$VLLM_PID"
  VLLM_PID=""
  # the GPU must be free before the next server or the training starts
  for _ in $(seq 1 60); do
    [[ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ]] && break
    sleep 2
  done
}
# stop_group <pid>: SIGTERM to the process group, then wait up to 2 min (lab eval closes its
# environments on SIGTERM)
stop_group() {
  [[ -n "$1" ]] && kill -0 -- "-$1" 2>/dev/null || return 0
  kill -TERM -- "-$1" 2>/dev/null || true
  # wait for every process of the group, not only its leader
  for _ in $(seq 1 120); do kill -0 -- "-$1" 2>/dev/null || return 0; sleep 1; done
  kill -KILL -- "-$1" 2>/dev/null || true
}
gpu_check() {
  "$GPY" -c "import torch; assert torch.cuda.is_available(), 'torch sees no CUDA device'; print('cuda ok:', torch.__version__, torch.cuda.get_device_name(0))"
}
# pack_results: everything small enough to download (no envs, recordings or checkpoints)
pack_results() {
  [[ -d "$LAB" ]] || return 0
  local rel="${LAB#"$ROOT"/}" out
  out="$ROOT/$(basename "$LAB")_results.tgz"
  tar czf "$out" -C "$ROOT" --exclude='.venv-gpu' --exclude='calls' --exclude='ckpt-*' --exclude='envs' \
    "$rel" 2>/dev/null && log "results packed: $out ($(du -h "$out" | cut -f1))"
}
cleanup() {
  stop_group "$STAGE_PID"
  stop_vllm
  stop_group "$TEACHER_PID"
}
on_exit() {
  local rc=$?
  cleanup
  if [[ "$PREPARE_ONLY" != 1 ]]; then pack_results || true; fi
  if [[ "${SHUTDOWN_WHEN_DONE:-0}" == 1 && "$PREPARE_ONLY" != 1 ]]; then
    log "SHUTDOWN_WHEN_DONE=1: shutting the machine down (exit code $rc)"
    sync
    shutdown -h now 2>/dev/null || /usr/bin/shutdown 2>/dev/null || true
  fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# start_vllm <log name> <extra vllm args...>
start_vllm() {
  local name="$1"; shift
  stop_vllm
  log "vllm up ($name)"
  setsid "$GPU_VENV/bin/vllm" serve "$MODEL_DIR" \
    --served-model-name "$SERVED" --host 127.0.0.1 --port "$PORT" \
    --max-model-len "$MAX_MODEL_LEN" --gpu-memory-utilization 0.90 --max-num-seqs 64 \
    --enable-auto-tool-choice --tool-call-parser hermes \
    "$@" >> "$LOGS/vllm-$name.log" 2>&1 &
  VLLM_PID=$!
  for _ in $(seq 1 180); do
    if curl -sf "$VLLM_URL/models" > /dev/null; then log "vllm ready ($name)"; return 0; fi
    kill -0 "$VLLM_PID" 2>/dev/null || { tail -n 40 "$LOGS/vllm-$name.log"; die "vllm ($name) exited"; }
    sleep 5
  done
  tail -n 40 "$LOGS/vllm-$name.log"
  die "vllm ($name) not ready after 15 min"
}

wb() { uv run --frozen workbench "$@"; }
count_success() { uv run --frozen python -c "import json, sys
try: print(sum(1 for l in open(sys.argv[1]) if l.strip() and json.loads(l).get('success')))
except FileNotFoundError: print(0)" "$1"; }
# jget <json file> <key>: one top-level value, empty if the file or key is missing
jget() { uv run --frozen python -c "import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))
except FileNotFoundError: print('')" "$1" "$2"; }

# eval_vllm <tag> <split> <model name> [extra lab eval args...]
eval_vllm() {
  local tag="$1" split="$2" model="$3"; shift 3
  wb lab eval --split "$split" --tag "$tag" --backend vllm --base-url "$VLLM_URL" --model "$model" \
    --no-thinking --concurrency "$CONC" --port-min 20000 --min-ok 0.5 "$@"
}

# ---------------------------------------------------------------------------- checks and setup
log "lab run in $ROOT"
# environment servers left behind by a run that was killed outright (kill -9, OOM, reboot)
pkill -TERM -f "${LAB#"$ROOT"/}/runs/[^ ]*/envs/" 2>/dev/null && { log "stopped servers left by an earlier run"; sleep 3; } || true
if [[ "$PREPARE_ONLY" != 1 ]]; then
  command -v nvidia-smi > /dev/null || die "nvidia-smi not found: a CUDA GPU is required"
  [[ -n "${DEEPSEEK_API_KEY:-}" ]] || die "export DEEPSEEK_API_KEY first (the teacher model)"
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
fi
free_gb=$(df -Pk "$ROOT" | awk 'NR==2 {print int($4/1024/1024)}')
log "free disk: ${free_gb} GB"
(( free_gb >= ${MIN_FREE_GB:-60} )) || die "less than ${MIN_FREE_GB:-60} GB free on $ROOT (models, two Python envs and the runs need it)"
command -v uv > /dev/null || { python3 -m pip install -q uv && export PATH="$HOME/.local/bin:$PATH"; }
command -v uv > /dev/null || die "uv not found and could not be installed"

setup_app() {
  git submodule update --init --depth 1 third_party/agent-world-model
  uv sync --frozen
}
setup_gpu() {
  [[ -x "$GPY" ]] || uv venv "$GPU_VENV" --python 3.12
  # a pip mirror configured on the machine (AutoDL has one) also serves this install
  local mirror
  mirror="$(python3 -m pip config get global.index-url 2>/dev/null || true)"
  if [[ -n "$mirror" && -z "${UV_DEFAULT_INDEX:-}" ]]; then export UV_DEFAULT_INDEX="$mirror"; fi
  # --no-config: the app's [tool.uv] constraints (numpy==2.4.2 for AWM) must not apply here;
  # vLLM 0.19.0 needs numba 0.61.2, hence numpy<2.3
  VIRTUAL_ENV="$GPU_VENV" uv pip install --no-config -r scripts/lab/gpu-requirements.txt
  "$GPY" -c "import torch, vllm, peft, sklearn; print(torch.__version__, vllm.__version__)"
  # without a GPU (PREPARE_ONLY on a CPU-only machine) CUDA is checked when the GPU run starts
  if nvidia-smi > /dev/null 2>&1; then gpu_check; fi
}
get_data() {
  [[ -f data/awm1k/gen_verifier.pure_code.jsonl ]] || bash scripts/download_data.sh
}
get_model() {
  "$GPU_VENV/bin/hf" download "$BASE_MODEL" --local-dir "$MODEL_DIR" \
    --exclude "*.pth" --exclude "original/*"
}
if [[ "${SKIP_SETUP:-0}" != 1 ]]; then
  stage setup-app setup_app
  stage setup-gpu setup_gpu
  stage data get_data
  stage model get_model
fi
if [[ "$PREPARE_ONLY" == 1 ]]; then
  log "prepared: environments, dataset and model are in place (PREPARE_ONLY=1, nothing else runs)"
  exit 0
fi
gpu_check || die "CUDA is not available in the GPU env (see the line above)"
# shellcheck disable=SC2086  # SPLIT_ARGS is a list of options
stage split wb lab split ${SPLIT_ARGS:-}

# -------------------------------------------- teacher episodes: API only, runs in the background
teacher() {
  local common=(--backend openai_compat --base-url "$TEACHER_BASE_URL" --model "$TEACHER_MODEL"
    --api-key-env DEEPSEEK_API_KEY --max-tokens 8192 --concurrency "$TEACHER_CONC" --port-min 40000)
  if [[ ! -f "$STAGES/teacher-train.done" ]]; then
    wb lab eval --split train --tag teacher-train "${common[@]}" ${TEACHER_TRAIN_LIMIT:+--limit "$TEACHER_TRAIN_LIMIT"} \
      --min-ok 0.5 || return 1
    touch "$STAGES/teacher-train.done"
  fi
  # too few solved train tasks: the teacher also runs the reserve train tasks
  if [[ ! -f "$STAGES/teacher-train-extra.done" ]]; then
    local solved
    solved="$(count_success "$LAB/runs/teacher-train/results.jsonl")"
    if (( solved < TEACHER_MIN_SUCCESS )); then
      echo "teacher solved $solved train tasks (< TEACHER_MIN_SUCCESS=$TEACHER_MIN_SUCCESS): reserve tasks too"
      wb lab eval --split train_extra --tag teacher-train-extra "${common[@]}" --min-ok 0.5 || return 1
    fi
    touch "$STAGES/teacher-train-extra.done"
  fi
  if [[ ! -f "$STAGES/teacher-test.done" ]]; then
    # the teacher is a reference on test, not a result: the first TEACHER_TEST_LIMIT tasks
    wb lab eval --split test --tag teacher-test "${common[@]}" --limit "$TEACHER_TEST_LIMIT" --min-ok 0.5 || return 1
    touch "$STAGES/teacher-test.done"
  fi
}
if [[ ! -f "$STAGES/teacher-test.done" ]]; then
  log "teacher episodes start in the background (log: $LOGS/teacher.log)"
  setsid bash -c "$(declare -f wb count_success teacher); STAGES='$STAGES' LAB='$LAB' \
    TEACHER_BASE_URL='$TEACHER_BASE_URL' TEACHER_MODEL='$TEACHER_MODEL' TEACHER_CONC='$TEACHER_CONC' \
    TEACHER_TRAIN_LIMIT='${TEACHER_TRAIN_LIMIT:-}' TEACHER_MIN_SUCCESS='$TEACHER_MIN_SUCCESS' \
    TEACHER_TEST_LIMIT='$TEACHER_TEST_LIMIT'; teacher" >> "$LOGS/teacher.log" 2>&1 &
  TEACHER_PID=$!
fi

# the verifier's floor: an agent that never answers (CPU only, no model). Test: reported; val:
# checkpoint selection ignores those tasks; train: their "successes" are not training data.
for s in test val train; do
  stage "null-$s" wb lab eval --split "$s" --tag "null-$s" --backend null --concurrency "$CONC" \
    --port-min 20000 --min-ok 0.5
done

# ----------------------------------------------------------------- base model: GPU, foreground
start_vllm base
stage smoke eval_vllm smoke val "$SERVED" --limit 8 --min-ok 0.5
stage base-test eval_vllm base-test test "$SERVED"
stage base-val eval_vllm base-val val "$SERVED"
stage base-passk eval_vllm base-passk test "$SERVED" --limit 150 --samples 4 --temperature 0.7
stage student-train eval_vllm student-train train "$SERVED" --samples 2 --temperature 0.7
stage inj-a eval_vllm inj-a inject "$SERVED" --inject --approver auto
stage inj-b eval_vllm inj-b inject "$SERVED" --inject --approver auto \
  --policy-file configs/lab/deny_destructive.yaml
stage inj-c eval_vllm inj-c inject "$SERVED" --inject --approver preview-guard
stage bench-prefix-on wb lab bench --label base-prefix-cache-on --model "$SERVED" --base-url "$VLLM_URL"
start_vllm base-no-prefix --no-enable-prefix-caching
stage bench-prefix-off wb lab bench --label base-prefix-cache-off --model "$SERVED" --base-url "$VLLM_URL"
stop_vllm

# ------------------------------------------------------------------------------ wait for teacher
if [[ -n "$TEACHER_PID" ]]; then
  log "waiting for the teacher episodes"
  wait "$TEACHER_PID" || true
  TEACHER_PID=""
fi
[[ -f "$STAGES/teacher-train.done" && -f "$STAGES/teacher-test.done" ]] || {
  tail -n 40 "$LOGS/teacher.log"; die "teacher episodes did not finish (rerun the script to resume them)"; }

# ---------------------------------------------------------------------------------- SFT variants
# reserve-task teacher episodes need their own null-agent run to find their trivial tasks
if [[ -d "$LAB/runs/teacher-train-extra" ]]; then
  stage null-train_extra wb lab eval --split train_extra --tag null-train_extra --backend null \
    --concurrency "$CONC" --port-min 20000 --min-ok 0.5
fi
TEACHER_RUNS=(--run teacher-train --run teacher-train-extra)
NOT_TRIVIAL=(--exclude-trivial null-train --exclude-trivial null-train_extra)
stage sft-data-teacher wb lab sft-data "${TEACHER_RUNS[@]}" "${NOT_TRIVIAL[@]}" \
  --out "$LAB/sft/teacher/train.jsonl"
stage sft-data-rft wb lab sft-data "${TEACHER_RUNS[@]}" --run student-train "${NOT_TRIVIAL[@]}" \
  --max-per-task 2 --out "$LAB/sft/rft/train.jsonl"
# as many episodes as the teacher variant: the ablation changes only whether they were verified
stage sft-data-unfiltered wb lab sft-data "${TEACHER_RUNS[@]}" "${NOT_TRIVIAL[@]}" --include-failed \
  --match "$LAB/sft/teacher/train.stats.json" --out "$LAB/sft/unfiltered/train.jsonl"

# a variant with too few episodes is skipped (and said so) rather than stopping the whole run
TRAINED=()
for v in "${VARIANTS[@]}"; do
  used="$(jget "$LAB/sft/$v/train.stats.json" used_episodes)"
  if (( ${used:-0} < MIN_SFT_EPISODES )); then
    log "skip training $v: $used episodes (< MIN_SFT_EPISODES=$MIN_SFT_EPISODES)"
    continue
  fi
  stage "train-$v" "$GPY" scripts/lab/sft_train.py --data "$LAB/sft/$v/train.jsonl" \
    --model "$MODEL_DIR" --out "$LAB/sft/$v/adapters"
  TRAINED+=("$v")
done

# --------------------------------------------- checkpoint selection on val, then test, one server
if (( ${#TRAINED[@]} > 0 )); then
  lora_args=()
  for v in "${TRAINED[@]}"; do
    for ck in "$LAB/sft/$v/adapters"/ckpt-*; do lora_args+=("$v-$(basename "$ck")=$ck"); done
  done
  start_vllm lora --enable-lora --max-lora-rank 64 --max-loras 2 --max-cpu-loras 16 \
    --lora-modules "${lora_args[@]}"
  for v in "${TRAINED[@]}"; do
    for ck in "$LAB/sft/$v/adapters"/ckpt-*; do
      c="$(basename "$ck")"
      stage "sft-$v-val-$c" eval_vllm "sft-$v-val-$c" val "$v-$c"
    done
    stage "select-$v" wb lab select --variant "$v"
    best="$(jget "$LAB/sft/$v/selected.json" checkpoint)"
    stage "sft-$v-test" eval_vllm "sft-$v-test" test "$v-$best"
  done
  # the variant with the best val score (selection never looks at test)
  best_model="$(uv run --frozen python - "$LAB" "${TRAINED[@]}" <<'PY'
import json, sys
lab, variants = sys.argv[1], sys.argv[2:]
sel = {v: json.load(open(f"{lab}/sft/{v}/selected.json")) for v in variants}
v = max(variants, key=lambda v: (sel[v]["val_rate"], -variants.index(v)))
print(f"{v}-{sel[v]['checkpoint']}")
PY
)"
  log "best adapter on val: $best_model"
  stage inj-sft-a eval_vllm inj-sft-a inject "$best_model" --inject --approver auto
  stage bench-lora wb lab bench --label lora-adapter --model "$best_model" --base-url "$VLLM_URL"
  stage bench-lora-base wb lab bench --label base-on-lora-server --model "$SERVED" --base-url "$VLLM_URL"
  stop_vllm
else
  log "no SFT variant had enough episodes; the LoRA stages are skipped"
fi

# ------------------------------------------------------------------------- analysis and report
mkdir -p "$LAB/risk"
stage risk "$GPY" scripts/lab/risk_model.py --lab-dir "$LAB" --out "$LAB/risk/risk.json" \
  --tags base-test base-val base-passk student-train teacher-train teacher-train-extra teacher-test \
  sft-teacher-test sft-rft-test sft-unfiltered-test
wb lab report
log "all stages finished: $LAB/REPORT.md"
