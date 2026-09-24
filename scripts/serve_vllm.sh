#!/usr/bin/env bash
# Serve Arctic-AWM with vLLM. UNVERIFIED-LOCAL: requires Linux x86_64 + CUDA GPU(s) and
# network access to huggingface.co. vLLM lives in the separate train env (rule R11), so this
# runs `vllm` from train/.venv (install it first: `cd train && uv sync`), or pass --docker to
# use the official vllm/vllm-openai image instead.
# The flags come from the profile; configs/serving/arctic-awm-4b.yaml enables
# --enable-auto-tool-choice --tool-call-parser hermes (owner decision D11, ADR-020), which the
# workbench agent's native-`tools` requests need; unverified on a GPU until Phase 15.
set -euo pipefail
PROFILE="${PROFILE:-configs/serving/arctic-awm-4b.yaml}"
MODE="${1:-local}"
mapfile -t ARGS < <(uv run workbench serve vllm-cmd --profile "$PROFILE" --args-only)
if [[ "$MODE" == "--docker" ]]; then
  # explicit entrypoint: do not rely on the image default (unverified)
  exec docker run --rm --gpus all -p 8000:8000 -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    --entrypoint vllm vllm/vllm-openai:v0.19.0 "${ARGS[@]:1}"
fi
exec uv run --project train "${ARGS[@]}"
