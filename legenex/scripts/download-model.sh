#!/usr/bin/env bash
# ============================================================================
# download-model.sh <hf_repo_id> <dest_dir> [min_free_gb] [revision]
#
# Resumable HuggingFace snapshot download via a throwaway container, so the host
# needs no pip packages. Refuses to start if it would eat the free-space reserve.
#
# `revision` (4th arg, or $REVISION) pins an immutable commit SHA. Pinning is
# strongly preferred over the floating default branch: it is what hf-verify.py
# records in .gx-manifest.json, so a later audit can prove which bytes are
# served. Omitting it downloads whatever `main` points at today.
# ============================================================================
set -euo pipefail

REPO="${1:?usage: download-model.sh <hf_repo_id> <dest_dir> [min_free_gb] [revision]}"
DEST="${2:?usage: download-model.sh <hf_repo_id> <dest_dir> [min_free_gb] [revision]}"
MIN_FREE_GB="${3:-80}"
REVISION="${4:-${REVISION:-}}"

parent="$(dirname "$DEST")"
mkdir -p "$parent"

free_gb=$(df -BG --output=avail "$parent" | tail -1 | tr -dc '0-9')
echo "[$(date -Is)] free space at ${parent}: ${free_gb}GB (reserve: ${MIN_FREE_GB}GB)"
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  echo "REFUSING: only ${free_gb}GB free, need at least ${MIN_FREE_GB}GB reserve" >&2
  exit 1
fi

echo "[$(date -Is)] downloading ${REPO}${REVISION:+@${REVISION}} -> ${DEST}"
# HF_TOKEN is passed through only if already present in the environment; it is
# never written to disk or logged.
docker run --rm \
  -v "${parent}:/out" \
  ${HF_TOKEN:+-e HF_TOKEN} \
  -e REPO_ID="${REPO}" \
  -e REVISION="${REVISION}" \
  -e LOCAL_DIR="/out/$(basename "$DEST")" \
  python:3.12-slim \
  sh -lc '
    pip install -q huggingface_hub hf_xet &&
    python -c "
import os
from huggingface_hub import snapshot_download
p = snapshot_download(
    repo_id=os.environ[\"REPO_ID\"],
    revision=os.environ[\"REVISION\"] or None,
    local_dir=os.environ[\"LOCAL_DIR\"],
    max_workers=8,
)
print(\"downloaded to\", p)
"
  '
echo "[$(date -Is)] done: $(du -sh "$DEST" | cut -f1) at ${DEST}"
