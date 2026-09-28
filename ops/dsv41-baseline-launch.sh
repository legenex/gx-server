#!/usr/bin/env bash
# Phase 15 headless transition + stock baseline launch (2026-09-27).
# Detached runner: survives the graphical-session termination that frees the
# memory the model load requires. Marker files record the outcome.
set -u
exec 9>/srv/logs/dsv41-baseline-launch.lock
flock -n 9 || exit 0

LOG=/srv/logs/dsv41-first-launch.log
MARK_OK=/srv/logs/dsv41-baseline-READY
MARK_FAIL=/srv/logs/dsv41-baseline-FAILED
REPO=/home/legenex/Documents/Projects/Server/gx-cluster
DRAIN="$REPO/ops/dsv41-prestart-drain.sh"

rm -f -- /srv/logs/dsv41-baseline-READY
rm -f -- /srv/logs/dsv41-baseline-FAILED

need()  { awk '/^MemAvailable:/ { printf "%.1f", $2 / 1048576 }' /proc/meminfo; }
ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'; }

{
  echo "[runner] $(date -Is) headless transition + baseline launch starting"

  sh=$(ls /srv/models/dsv41/model/model-*.safetensors 2>/dev/null | wc -l)
  if [ "$sh" -lt 39 ]; then echo "[runner] ERROR pack incomplete ($sh/39)"; echo "pack incomplete" > "$MARK_FAIL"; exit 1; fi
  for f in model-00047-of-00048.safetensors model-00048-of-00048.safetensors model.safetensors.index.json config.json; do
    [ -s "/srv/models/dsv41/engram-src/$f" ] || { echo "[runner] ERROR engram missing $f"; echo "engram incomplete" > "$MARK_FAIL"; exit 1; }
  done

  echo "[runner] draining non-critical consumers + graphical/RDP"
  if ! bash "$DRAIN"; then
    echo "[runner] ERROR drain failed MemAvailable=$(need)"
    echo "drain failed: $(need)" > "$MARK_FAIL"
    exit 1
  fi

  AV=$(need); echo "[runner] MemAvailable now ${AV} GiB"
  if ! ge "$AV" "112"; then
    echo "[runner] ERROR memory did not free"
    echo "memory did not free after drain: $AV" > "$MARK_FAIL"
    exit 1
  fi

  cd "$REPO/mia-dsv41" || { echo pack-dir-missing > "$MARK_FAIL"; exit 1; }
  ./start.sh
  RC=$?
  echo "[runner] start.sh exited rc=$RC"

  docker ps --format '{{.Names}} {{.Status}}' | grep -i dsv41
  sleep 5
  H=$(curl -s -m 10 http://127.0.0.1:8888/health)
  echo "[runner] /health: $H"
  M=$(curl -s -m 10 http://127.0.0.1:8888/v1/models)
  echo "[runner] /v1/models: ${M:0:300}"
  R=$(curl -s -m 60 http://127.0.0.1:8888/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"DeepSeek-v4.1-Flash-EXL3","messages":[{"role":"user","content":"17*19=? Answer with just the number."}],"max_tokens":16,"chat_template_kwargs":{"enable_thinking":false}}')
  echo "[runner] probe: $R" | head -c 600
  if [ "$RC" = 0 ] && echo "$R" | grep -q "323" && echo "$M" | grep -q "DeepSeek-v4.1-Flash-EXL3"; then
    echo "ready $(date -Is)" > "$MARK_OK"
    echo "[runner] BASELINE READY. Memory after load: $(need) GiB"
  else
    echo "start.sh rc=$RC probe=$R" > "$MARK_FAIL"
    echo "[runner] BASELINE FAILED — see $LOG"
  fi
} >> "$LOG" 2>&1
