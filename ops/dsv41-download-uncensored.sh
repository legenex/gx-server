#!/usr/bin/env bash
set -euo pipefail
DEST=/srv/models/dsv41/uncensored
REPO=dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw
REV=8a27b35fc5b145fa05ee965c7d7b243b047915f7
LOG=/srv/logs/dsv41-uncensored-download.log
mkdir -p "$DEST" /srv/logs
export HF_XET_HIGH_PERFORMANCE=1
{
  echo "[dl] $(date -Is) start $REPO @$REV -> $DEST"
  df -h /
  hf download "$REPO" --revision "$REV" --local-dir "$DEST"
  echo "[dl] $(date -Is) finished"
  find "$DEST" -name 'model-*.safetensors' | wc -l
  df -h /
} >>"$LOG" 2>&1
