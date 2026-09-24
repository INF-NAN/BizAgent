#!/usr/bin/env bash
# Download the official AgentWorldModel-1K dataset into data/awm1k/ (gitignored; rule R6).
# License: CC-BY-4.0 (attribution in README.md and docs/UPSTREAM.md section 5).
# Requires network access to huggingface.co and its download CDN; HF_TOKEN is optional.
# Verified on 2026-09-24 (docs/verification/2026-09-24-dataset.md).
set -euo pipefail
DEST="${1:-data/awm1k}"
REPO="Snowflake/AgentWorldModel-1K"
mkdir -p "$DEST"
# Resolve the current dataset commit first so the download and the manifest refer to the same revision.
REV="${AWM1K_REVISION:-$(uv run python -c "from huggingface_hub import HfApi; print(HfApi().dataset_info('$REPO').sha)")}"
uv run hf download "$REPO" --repo-type dataset --revision "$REV" --local-dir "$DEST"
cat > "$DEST/MANIFEST.json" <<JSON
{"origin": "official", "source": "huggingface.co/datasets/$REPO", "revision": "$REV", "license": "CC-BY-4.0", "downloaded_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "modify": "never (rule R2)"}
JSON
echo "dataset ready in $DEST (revision $REV)"
