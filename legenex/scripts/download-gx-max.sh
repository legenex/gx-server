#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="lmsysorg/sglang:dev-v4f-2dgx-v2"
MODEL_REPO="nvidia/DeepSeek-V4-Flash-0731-NVFP4"

MODEL_ROOT="/srv/models/deepseek"
MODEL_DIR="${MODEL_ROOT}/DeepSeek-V4-Flash-0731-NVFP4"

REMOTE_USER="legenex-02"
REMOTE_IP="192.168.100.11"
SSH_KEY="/home/legenex/.ssh/id_ed25519_gxcluster"

LOG="/srv/logs/gx-max-download.log"

mkdir -p /srv/logs
mkdir -p "$MODEL_DIR"

exec > >(tee -a "$LOG") 2>&1

SSH_CMD=(
  ssh
  -i "$SSH_KEY"
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=accept-new
  "${REMOTE_USER}@${REMOTE_IP}"
)

echo
echo "======================================================"
echo " GX-MAX OVERNIGHT SETUP"
echo " $(date)"
echo "======================================================"
echo

echo "MODEL:"
echo "$MODEL_REPO"
echo

echo "----- LOCAL DISK -----"
df -h /srv

echo
echo "----- GX10-02 DISK -----"
"${SSH_CMD[@]}" 'df -h /srv'

REQUIRED_BYTES=220000000000

LOCAL_FREE=$(df -PB1 /srv | awk 'NR==2 {print $4}')
REMOTE_FREE=$("${SSH_CMD[@]}" "df -PB1 /srv | awk 'NR==2 {print \$4}'")

if [ "$LOCAL_FREE" -lt "$REQUIRED_BYTES" ]; then
    echo "ERROR: GX10-01 has less than 220 GB free."
    exit 1
fi

if [ "$REMOTE_FREE" -lt "$REQUIRED_BYTES" ]; then
    echo "ERROR: GX10-02 has less than 220 GB free."
    exit 1
fi

echo
echo "Disk check PASSED."
echo

echo "======================================================"
echo " STEP 1: Pulling SGLang Spark image on GX10-01"
echo "======================================================"

docker pull "$IMAGE"

echo
echo "======================================================"
echo " STEP 2: Pulling SGLang image on GX10-02 in background"
echo "======================================================"

"${SSH_CMD[@]}" "docker pull '$IMAGE'" &
REMOTE_PULL_PID=$!

echo
echo "======================================================"
echo " STEP 3: Downloading DeepSeek V4 Flash NVFP4"
echo " Destination: $MODEL_DIR"
echo "======================================================"

docker run --rm \
  --entrypoint python3 \
  -v "${MODEL_ROOT}:/models" \
  "$IMAGE" \
  -c '
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="nvidia/DeepSeek-V4-Flash-0731-NVFP4",
    local_dir="/models/DeepSeek-V4-Flash-0731-NVFP4",
    max_workers=8
)
'

echo
echo "======================================================"
echo " MODEL DOWNLOAD COMPLETE"
echo "======================================================"

du -sh "$MODEL_DIR"

echo
echo "Waiting for GX10-02 SGLang image pull..."

if wait "$REMOTE_PULL_PID"; then
    echo "GX10-02 SGLang image pull complete."
else
    echo "WARNING: GX10-02 image pull failed."
    echo "The model sync will continue."
fi

echo
echo "======================================================"
echo " STEP 4: Preparing GX10-02 model directory"
echo "======================================================"

"${SSH_CMD[@]}" \
  "mkdir -p '$MODEL_DIR'"

echo
echo "======================================================"
echo " STEP 5: Copying 176 GB model over ConnectX"
echo " GX10-01 -> 192.168.100.11"
echo "======================================================"

if command -v rsync >/dev/null 2>&1 && \
   "${SSH_CMD[@]}" 'command -v rsync >/dev/null 2>&1'; then

    rsync \
      -a \
      --whole-file \
      --partial \
      --info=progress2 \
      -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
      "${MODEL_DIR}/" \
      "${REMOTE_USER}@${REMOTE_IP}:${MODEL_DIR}/"

else

    echo "rsync unavailable. Falling back to tar over SSH."

    tar -C "$MODEL_DIR" -cf - . | \
      "${SSH_CMD[@]}" "tar -C '$MODEL_DIR' -xf -"

fi

echo
echo "======================================================"
echo " STEP 6: VERIFYING"
echo "======================================================"

echo
echo "GX10-01:"
du -sh "$MODEL_DIR"
find "$MODEL_DIR" -type f | wc -l

echo
echo "GX10-02:"
"${SSH_CMD[@]}" \
  "du -sh '$MODEL_DIR'; find '$MODEL_DIR' -type f | wc -l"

echo
echo "======================================================"
echo " GX-MAX DOWNLOAD + SYNC COMPLETE"
echo " $(date)"
echo "======================================================"
