#!/usr/bin/env bash
# Detached Gate A runner: drain -> start -> probe -> stop -> restart -> probe.
# Survives graphical/SSH-IDE session teardown. Markers record the outcome.
set -u
exec 9>/srv/logs/dsv41-baseline-launch.lock
flock -n 9 || exit 0

LOG=/srv/logs/dsv41-first-launch.log
MARK_OK=/srv/logs/dsv41-baseline-READY
MARK_FAIL=/srv/logs/dsv41-baseline-FAILED
REPO=/home/legenex/Documents/Projects/Server/gx-cluster
DRAIN="$REPO/ops/dsv41-prestart-drain.sh"
MIA="$REPO/mia-dsv41"
WORKER_SSH=legenex-02@10.60.21.41

rm -f -- /srv/logs/dsv41-baseline-READY
rm -f -- /srv/logs/dsv41-baseline-FAILED

need()  { awk '/^MemAvailable:/ { printf "%.1f", $2 / 1048576 }' /proc/meminfo; }
worker_need() { ssh -T -o BatchMode=yes -o ConnectTimeout=10 "$WORKER_SSH" "awk '/^MemAvailable:/ { printf \"%.1f\", \$2 / 1048576 }' /proc/meminfo" 2>/dev/null || echo "?"; }
ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'; }

fail() {
  echo "$*" > "$MARK_FAIL"
  echo "[runner] BASELINE FAILED — $*"
  exit 1
}

probe() {
  local tag="$1"
  local H M R
  sleep 3
  local HC
  HC=$(curl -s -m 15 -o /tmp/dsv41-health.body -w '%{http_code}' http://127.0.0.1:8888/health || echo 000)
  H=$(cat /tmp/dsv41-health.body 2>/dev/null || true)
  echo "[runner] $tag /health: HTTP $HC body=$H"
  M=$(curl -s -m 15 http://127.0.0.1:8888/v1/models || true)
  echo "[runner] $tag /v1/models: ${M:0:300}"
  R=$(curl -s -m 90 http://127.0.0.1:8888/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"DeepSeek-v4.1-Flash-EXL3","messages":[{"role":"user","content":"Return only the result of 17 multiplied by 19."}],"max_tokens":16,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}' || true)
  echo "[runner] $tag probe: $R" | head -c 800
  echo
  [ "$HC" = "200" ] || return 1
  echo "$M" | grep -q "DeepSeek-v4.1-Flash-EXL3" || return 1
  echo "$R" | grep -q "323" || return 1
  return 0
}

{
  echo "[runner] $(date -Is) Gate A drain + start + stop + restart beginning"
  echo "[runner] head MemAvailable=$(need) GiB worker=$(worker_need) GiB"

  sh=$(ls /srv/models/dsv41/model/model-*.safetensors 2>/dev/null | wc -l)
  if [ "$sh" -lt 39 ]; then fail "pack incomplete ($sh/39)"; fi
  for f in model-00047-of-00048.safetensors model-00048-of-00048.safetensors model.safetensors.index.json config.json; do
    [ -s "/srv/models/dsv41/engram-src/$f" ] || fail "engram missing $f"
  done

  echo "[runner] draining non-critical consumers + graphical/RDP + stale closing sessions"
  bash "$DRAIN" || fail "drain failed MemAvailable=$(need)"

  AV=$(need); echo "[runner] MemAvailable now ${AV} GiB worker=$(worker_need) GiB"
  ge "$AV" "112" || fail "memory did not free after drain: $AV"

  cd "$MIA" || fail "pack-dir-missing"

  echo "[runner] === START 1 ==="
  ./start.sh
  RC=$?
  echo "[runner] start.sh #1 exited rc=$RC"
  docker ps --format '{{.Names}} {{.Status}}' | grep -i dsv41 || true
  [ "$RC" = 0 ] || fail "start.sh #1 rc=$RC"
  probe "boot1" || fail "boot1 probe failed"

  echo "[runner] === STOP 1 ==="
  ./stop.sh || fail "stop.sh failed"
  sleep 5
  H1=$(need); W1=$(worker_need)
  echo "[runner] after stop: head=${H1} GiB worker=${W1} GiB"
  docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'dsv41-exl3-head|dsv41-exl3-worker' || echo "[runner] no stale rank containers"
  ge "$H1" "100" || fail "head memory did not return after stop: $H1"
  awk -v a="$W1" 'BEGIN { exit !(a + 0 >= 100) }' || fail "worker memory did not return after stop: $W1"

  echo "[runner] draining again before restart"
  bash "$DRAIN" || fail "drain before restart failed"
  AV=$(need); echo "[runner] pre-restart MemAvailable ${AV} GiB worker=$(worker_need) GiB"
  ge "$AV" "112" || fail "memory too low for restart: $AV"

  echo "[runner] === START 2 ==="
  ./start.sh
  RC=$?
  echo "[runner] start.sh #2 exited rc=$RC"
  [ "$RC" = 0 ] || fail "start.sh #2 rc=$RC"
  probe "boot2" || fail "boot2 probe failed"

  echo "ready $(date -Is) head=$(need) worker=$(worker_need)" > "$MARK_OK"
  echo "[runner] GATE A READY. Memory after second load: head=$(need) GiB worker=$(worker_need) GiB"
} >> "$LOG" 2>&1
