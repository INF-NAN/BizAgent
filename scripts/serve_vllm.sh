#!/usr/bin/env bash
# Serve Arctic-AWM with vLLM from the separate train env. Needs Linux x86_64 + a CUDA GPU and
# network access to huggingface.co (or HF_ENDPOINT). vLLM lives in train/.venv because the
# training stack's pins conflict with AWM's (ADR-002); install it first, see docs/DEPLOYMENT.md.
# For a containerized server use `docker compose --profile gpu up vllm`.
# The flags come from the serving profile; configs/serving/arctic-awm-4b.yaml enables
# --enable-auto-tool-choice --tool-call-parser hermes (ADR-018), which the workbench agent's
# native-`tools` requests need.
set -euo pipefail
PROFILE="${PROFILE:-configs/serving/arctic-awm-4b.yaml}"
mapfile -t ARGS < <(uv run workbench serve vllm-cmd --profile "$PROFILE" --args-only)
exec uv run --project train "${ARGS[@]}"
