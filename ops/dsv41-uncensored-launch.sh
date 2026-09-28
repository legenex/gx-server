#!/usr/bin/env bash
set -u
LOG=/srv/logs/dsv41-uncensored-launch.log
MARK_OK=/srv/logs/dsv41-uncensored-READY
MARK_FAIL=/srv/logs/dsv41-uncensored-FAILED
REPO=/home/legenex/Documents/Projects/Server/gx-cluster
exec 9>/srv/logs/dsv41-uncensored-launch.lock
flock -n 9 || exit 0
rm -f -- "$MARK_OK" "$MARK_FAIL"
{
  echo "[u] $(date -Is) uncensored launch"
  bash "$REPO/ops/dsv41-prestart-drain.sh" || { echo drain-failed > "$MARK_FAIL"; exit 1; }
  sh=$(ls /srv/models/dsv41/uncensored/model-*.safetensors 2>/dev/null | wc -l)
  [ "$sh" -ge 39 ] || { echo "shards $sh" > "$MARK_FAIL"; exit 1; }
  cd "$REPO/mia-dsv41" || { echo no-mia > "$MARK_FAIL"; exit 1; }
  ./start.sh
  RC=$?
  echo "[u] start.sh rc=$RC"
  HC=$(curl -s -m 15 -o /tmp/dsv41-u-health.body -w '%{http_code}' http://127.0.0.1:8888/health || echo 000)
  M=$(curl -s -m 15 http://127.0.0.1:8888/v1/models || true)
  R=$(curl -s -m 90 http://127.0.0.1:8888/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"DeepSeek-v4.1-Flash-EXL3","messages":[{"role":"user","content":"Return only the result of 17 multiplied by 19."}],"max_tokens":16,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}' || true)
  echo "[u] health=$HC models=${M:0:200}"
  echo "[u] probe=$R" | head -c 500
  echo
  if [ "$RC" = 0 ] && [ "$HC" = "200" ] && echo "$R" | grep -q 323; then
    echo "ready $(date -Is)" > "$MARK_OK"
    echo "[u] UNCENSORED READY"
  else
    echo "rc=$RC health=$HC" > "$MARK_FAIL"
    echo "[u] FAILED"
    exit 1
  fi
} >>"$LOG" 2>&1
