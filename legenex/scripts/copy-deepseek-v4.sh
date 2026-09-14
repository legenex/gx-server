#!/usr/bin/env bash
set -euo pipefail

SRC="/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4/"
DEST="/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4/"
REMOTE="legenex-02@192.168.100.11"
KEY="/home/legenex/.ssh/id_ed25519_gxcluster"
LOG="/srv/logs/deepseek-v4-copy.log"

exec > >(tee -a "$LOG") 2>&1

echo "DeepSeek V4 copy started: $(date)"

ssh \
  -i "$KEY" \
  -o IdentitiesOnly=yes \
  "$REMOTE" \
  "mkdir -p '$DEST'"

rsync \
  -aH \
  --whole-file \
  --partial \
  --info=progress2 \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=10" \
  "$SRC" \
  "$REMOTE:$DEST"

echo
echo "GX10-01:"
du -sh "$SRC"
find "$SRC" -type f | wc -l

echo
echo "GX10-02:"
ssh \
  -i "$KEY" \
  -o IdentitiesOnly=yes \
  "$REMOTE" \
  "du -sh '$DEST'; find '$DEST' -type f | wc -l"

echo
echo "DEEPSEEK V4 COPY COMPLETE"
echo "Finished: $(date)"
