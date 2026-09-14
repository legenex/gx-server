#!/usr/bin/env bash
set -u

IMAGE="lmsysorg/sglang:dev-v4f-2dgx-v2"
ROOT="/srv/models/deepseek"
DIR="$ROOT/DeepSeek-V4-Flash-0731-NVFP4"

REMOTE="legenex-02@192.168.100.11"
KEY="/home/legenex/.ssh/id_ed25519_gxcluster"
LOG="/srv/logs/gx-max-overnight.log"

mkdir -p "$DIR" /srv/logs
exec > >(tee -a "$LOG") 2>&1

echo "GX-MAX overnight job started: $(date)"

echo "Waiting for SGLang image on GX10-01..."
until docker image inspect "$IMAGE" >/dev/null 2>&1; do
  sleep 20
done

echo "GX10-01 SGLang image ready."

echo "Waiting for SGLang image on GX10-02..."
until ssh -i "$KEY" -o IdentitiesOnly=yes "$REMOTE" \
  "docker image inspect '$IMAGE' >/dev/null 2>&1"; do
  sleep 20
done

echo "GX10-02 SGLang image ready."

echo "Downloading DeepSeek V4 Flash..."

until docker run --rm \
  --entrypoint python3 \
  -v "$ROOT:/models" \
  "$IMAGE" \
  -c '
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="nvidia/DeepSeek-V4-Flash-0731-NVFP4",
    local_dir="/models/DeepSeek-V4-Flash-0731-NVFP4",
    max_workers=8
)
'; do
  echo "Download interrupted. Retrying in 60 seconds..."
  sleep 60
done

echo "Model download complete."
du -sh "$DIR"

ssh -i "$KEY" -o IdentitiesOnly=yes "$REMOTE" \
  "mkdir -p '$DIR'"

echo "Copying model to GX10-02 over ConnectX..."

until rsync -aH --partial --info=progress2 \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=10" \
  "$DIR/" \
  "$REMOTE:$DIR/"; do
  echo "Copy interrupted. Retrying in 30 seconds..."
  sleep 30
done

echo "GX10-01:"
du -sh "$DIR"

echo "GX10-02:"
ssh -i "$KEY" -o IdentitiesOnly=yes "$REMOTE" \
  "du -sh '$DIR'"

echo "GX-MAX DOWNLOAD + SYNC COMPLETE"
echo "Finished: $(date)"
