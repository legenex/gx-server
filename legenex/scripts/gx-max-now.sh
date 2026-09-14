#!/usr/bin/env bash
set -u

IMAGE="lmsysorg/sglang:dev-v4f-2dgx-v2"

ROOT="/srv/models/deepseek"
DIR="$ROOT/DeepSeek-V4-Flash-0731-NVFP4"

REMOTE="legenex-02@192.168.100.11"
KEY="/home/legenex/.ssh/id_ed25519_gxcluster"

LOG="/srv/logs/gx-max-now.log"

mkdir -p "$DIR" /srv/logs

exec > >(tee -a "$LOG") 2>&1

echo
echo "======================================================"
echo "GX-MAX INSTALL"
echo "Started: $(date)"
echo "======================================================"

echo
echo "STARTING DEEPSEEK V4 FLASH DOWNLOAD NOW"
echo

until docker run --rm \
  -v "$ROOT:/models" \
  python:3.12-slim \
  sh -lc '
    pip install -q huggingface_hub hf_xet &&
    python - <<PY
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="nvidia/DeepSeek-V4-Flash-0731-NVFP4",
    local_dir="/models/DeepSeek-V4-Flash-0731-NVFP4",
    max_workers=8
)
PY
  '
do
    echo
    echo "Download interrupted."
    echo "Retrying in 60 seconds..."
    sleep 60
done

echo
echo "======================================================"
echo "DEEPSEEK DOWNLOAD COMPLETE"
echo "======================================================"

du -sh "$DIR"

echo
echo "Waiting for GX10-01 SGLang image..."

until docker image inspect "$IMAGE" >/dev/null 2>&1
do
    sleep 30
done

echo "GX10-01 SGLang image ready."

echo
echo "Waiting for GX10-02 SGLang image..."

until ssh \
  -i "$KEY" \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  "$REMOTE" \
  "docker image inspect '$IMAGE' >/dev/null 2>&1"
do
    sleep 30
done

echo "GX10-02 SGLang image ready."

echo
echo "Copying DeepSeek to GX10-02 over ConnectX..."

ssh \
  -i "$KEY" \
  -o IdentitiesOnly=yes \
  "$REMOTE" \
  "mkdir -p '$DIR'"

until rsync \
  -aH \
  --partial \
  --info=progress2 \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=10" \
  "$DIR/" \
  "$REMOTE:$DIR/"
do
    echo
    echo "Copy interrupted."
    echo "Retrying in 30 seconds..."
    sleep 30
done

echo
echo "======================================================"
echo "VERIFYING"
echo "======================================================"

echo "GX10-01:"
du -sh "$DIR"

echo
echo "GX10-02:"
ssh \
  -i "$KEY" \
  -o IdentitiesOnly=yes \
  "$REMOTE" \
  "du -sh '$DIR'"

echo
echo "======================================================"
echo "GX-MAX DOWNLOAD + SYNC COMPLETE"
echo "Finished: $(date)"
echo "======================================================"
