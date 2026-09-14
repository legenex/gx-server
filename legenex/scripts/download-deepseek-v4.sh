#!/usr/bin/env bash
set -euo pipefail

MODEL_ROOT="/srv/models/deepseek"
MODEL_DIR="$MODEL_ROOT/DeepSeek-V4-Flash-0731-NVFP4"
LOG="/srv/logs/deepseek-v4-download.log"

mkdir -p "$MODEL_DIR" /srv/logs

exec > >(tee -a "$LOG") 2>&1

echo "======================================================"
echo "DeepSeek V4 Flash download started: $(date)"
echo "======================================================"

docker run --rm \
  -v "$MODEL_ROOT:/models" \
  python:3.12-slim \
  sh -lc '
    pip install --no-cache-dir -q huggingface_hub hf_xet &&
    python - <<PY
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="nvidia/DeepSeek-V4-Flash-0731-NVFP4",
    local_dir="/models/DeepSeek-V4-Flash-0731-NVFP4",
    max_workers=8
)
PY
  '

echo
echo "======================================================"
echo "DEEPSEEK V4 DOWNLOAD COMPLETE"
du -sh "$MODEL_DIR"
echo "Finished: $(date)"
echo "======================================================"
