#!/usr/bin/env bash
# gx-music lifecycle acceptance on node 2 (REAL engine). Requires the
# supervisor to run with a short GX_MUSIC_IDLE_UNLOAD_S (e.g. 60).
#   1. load via a job -> idle unload happens by itself, memory returns, ledger clean
#   2. load again -> gx-max hold appears -> engine torn down within ~20 s,
#      a new job waits with a gx-max reason -> hold removed -> job completes
#   3. explicit unload; no container, no ledger entry, no api_audio leftovers
set -uo pipefail
B="${B:-http://127.0.0.1:18820}"
K="$(cat /srv/projects/gx-cluster/secrets/gx-music/api-key)"
HOLD=/srv/projects/gx-cluster/state/guard/node2.gxmax-hold
LEDGER=/srv/projects/gx-cluster/state/guard/node2-residency.json
api() { curl -sS -H "Authorization: Bearer $K" -H 'Content-Type: application/json' "$@"; }
avail() { awk '/MemAvailable/ {printf "%.1f", $2/1048576}' /proc/meminfo; }
engine_state() { curl -s "$B/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["engine"])'; }
job_status() { api "$B/v1/music/$1" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(j["status"], "|", j["detail"])'; }
submit() {
  api -X POST "$B/v1/music/generations" -d '{"prompt":"lifecycle test pad","instrumental":true,"duration":10,
    "seed":3,"thinking":false,"bpm":90,"key":"C major","time_signature":"4"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}
wait_status() { # job regex timeout
  local t=0; while [ $t -lt "$3" ]; do s=$(job_status "$1"); [[ "$s" =~ $2 ]] && { echo "$s"; return 0; }; sleep 2; t=$((t+2)); done
  echo "TIMEOUT: $s"; return 1
}
ok=0; fail=0
check() { if eval "$2"; then echo "PASS  $1"; ok=$((ok+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

echo "== 1. idle unload"
rm -f "$HOLD"
echo "  avail before: $(avail) GiB"
j=$(submit); wait_status "$j" '^completed' 600
loaded_avail=$(avail); echo "  avail loaded: $loaded_avail GiB; engine=$(engine_state)"
t0=$(date +%s); while [ "$(engine_state)" != unloaded ] && [ $(( $(date +%s) - t0 )) -lt 180 ]; do sleep 5; done
idle_s=$(( $(date +%s) - t0 ))
sleep 5
echo "  engine unloaded ${idle_s}s after completion; avail now $(avail) GiB"
check "idle unload happened (<=180 s)" '[ "$(engine_state)" = unloaded ]'
check "no gx-music container after idle unload" '! docker inspect gx-music >/dev/null 2>&1'
check "ledger has no gx-music" '! grep -q gx-music "$LEDGER" 2>/dev/null'
check "memory returned (>= +15 GiB vs loaded)" 'python3 -c "import sys; sys.exit(0 if $(avail) - $loaded_avail >= 15 else 1)"'

echo "== 2. gx-max hold drains a loaded engine and blocks new work"
j=$(submit); wait_status "$j" '^completed' 600
check "engine ready before hold" '[ "$(engine_state)" = ready ]'
touch "$HOLD"; t0=$(date +%s)
while [ "$(engine_state)" != unloaded ] && [ $(( $(date +%s) - t0 )) -lt 60 ]; do sleep 2; done
drain_s=$(( $(date +%s) - t0 ))
echo "  engine torn down ${drain_s}s after hold"
check "hold tears down engine within 30 s" '[ "$drain_s" -le 30 ] && ! docker inspect gx-music >/dev/null 2>&1'
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $K" -X POST "$B/v1/music/load")
check "explicit load refused while held (503)" '[ "$code" = 503 ]'
j2=$(submit); s=$(wait_status "$j2" '^waiting_for_resource' 60); echo "  $s"
check "new job waits with a gx-max reason" '[[ "$s" == *gx-max* ]]'
sleep 20
check "engine still not started while held" '! docker inspect gx-music >/dev/null 2>&1'
rm -f "$HOLD"
s=$(wait_status "$j2" '^completed' 900); echo "  $s"
check "job completes after hold released" '[[ "$s" == completed* ]]'

echo "== 3. explicit unload and cleanliness"
api -X POST "$B/v1/music/unload" >/dev/null; sleep 5
check "no container" '! docker inspect gx-music >/dev/null 2>&1'
check "no ledger entry" '! grep -q gx-music "$LEDGER" 2>/dev/null'
check "engine scratch empty" '[ -z "$(ls -A /srv/models/music-data/api_audio 2>/dev/null)" ]'
check "no stray engine processes" '! pgrep -f "acestep.api_server|gx_engine_entry" >/dev/null'
echo "  final avail: $(avail) GiB"
echo "RESULT pass=$ok fail=$fail"
[ "$fail" = 0 ]
