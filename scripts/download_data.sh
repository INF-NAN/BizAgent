#!/usr/bin/env bash
# Download the official AgentWorldModel-1K dataset into data/awm1k/ (gitignored; rule R6).
# UNVERIFIED-LOCAL in the development sandbox: huggingface.co is blocked by its egress policy.
# Requires network access to huggingface.co (and its download CDN); HF_TOKEN is optional.
set -euo pipefail
DEST="${1:-data/awm1k}"
mkdir -p "$DEST"
uv run hf download Snowflake/AgentWorldModel-1K --repo-type dataset --local-dir "$DEST"
cat > "$DEST/MANIFEST.json" <<JSON
{"origin": "official", "source": "huggingface.co/datasets/Snowflake/AgentWorldModel-1K", "downloaded_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "modify": "never (rule R2)"}
JSON
echo "dataset ready in $DEST"
