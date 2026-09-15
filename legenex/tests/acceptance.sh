#!/usr/bin/env bash
# ============================================================================
# acceptance.sh — end-to-end acceptance suite for the gx-cluster.
#
# Exercises the REAL system through the single gateway endpoint. Nothing here
# is mocked: every "PASS" means a model actually produced output.
#
# Usage:
#   legenex/tests/acceptance.sh [test-name ...]
#   legenex/tests/acceptance.sh              # run all except the slow ones
#   GX_RUN_SLOW=1 legenex/tests/acceptance.sh   # include gx-max + lifecycle
#
# Exit code is the number of failed tests.
# ============================================================================
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${repo}/legenex/gateway/.env"
GATEWAY="${GX_GATEWAY:-http://127.0.0.1:4000}"
ORCH="${GX_ORCH:-http://127.0.0.1:18900}"

if [ -f "${ENV_FILE}" ]; then
  KEY="$(grep '^LITELLM_MASTER_KEY=' "${ENV_FILE}" | cut -d= -f2-)"
else
  KEY="${LITELLM_MASTER_KEY:-}"
fi
[ -n "${KEY}" ] || { echo "no gateway key found (LITELLM_MASTER_KEY)"; exit 99; }

PASS=0; FAIL=0; SKIP=0
RESULTS=()

pass(){ PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail(){ FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1 :: $2"); printf '  \033[31mFAIL\033[0m  %s\n        %s\n' "$1" "$2"; }
skip(){ SKIP=$((SKIP+1)); RESULTS+=("SKIP  $1 :: $2"); printf '  \033[33mSKIP\033[0m  %s (%s)\n' "$1" "$2"; }

chat(){ # chat <model> <json-body-file|-> ; echoes response to stdout
  curl -s -m "${GX_TIMEOUT:-900}" "${GATEWAY}/v1/chat/completions" \
    -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
    --data-binary @"$1"
}

body(){ # body <model> <prompt> [max_tokens] -> writes /tmp/gxacc_body.json
  python3 - "$1" "$2" "${3:-120}" <<'PY' > /tmp/gxacc_body.json
import json,sys
print(json.dumps({"model":sys.argv[1],"max_tokens":int(sys.argv[3]),"temperature":0,
                  "messages":[{"role":"user","content":sys.argv[2]}]}))
PY
}

content(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
if 'error' in d: print('__ERROR__'+json.dumps(d['error'])[:300])
else: print((d['choices'][0]['message'].get('content') or '').replace(chr(10),' '))
" "$1"; }

# ---------------------------------------------------------------- 0. gateway
t_gateway(){
  echo "[0] gateway and aliases"
  local models
  models=$(curl -s -m 15 "${GATEWAY}/v1/models" -H "Authorization: Bearer ${KEY}" \
    | python3 -c "import sys,json;print(' '.join(sorted(m['id'] for m in json.load(sys.stdin).get('data',[]))))" 2>/dev/null)
  local want="gx-auto gx-fast gx-image gx-max gx-mini gx-reason gx-video"
  if [ "${models}" = "${want}" ]; then pass "all 7 aliases exposed"; else fail "alias list" "got: ${models:-<none>}"; fi

  curl -fsS -m 10 "${ORCH}/health" >/dev/null 2>&1 \
    && pass "orchestrator healthy" || fail "orchestrator health" "no 200 from ${ORCH}/health"
}

# ------------------------------------------------------------------ 1. mini
t_mini(){
  echo "[1] gx-mini"
  body gx-mini "In one sentence, what is RoCE?" 120
  chat /tmp/gxacc_body.json > /tmp/gxacc_mini.json
  local c; c=$(content /tmp/gxacc_mini.json)
  if [[ "$c" == __ERROR__* ]]; then fail "gx-mini text" "${c:9:200}"
  elif [ ${#c} -lt 20 ]; then fail "gx-mini text" "empty/short response: '${c}'"
  else pass "gx-mini text inference"; fi

  if [ -f /tmp/gxacc_vision.png ]; then
    python3 - <<'PY' > /tmp/gxacc_body.json
import base64,json
b=base64.b64encode(open('/tmp/gxacc_vision.png','rb').read()).decode()
print(json.dumps({"model":"gx-mini","max_tokens":150,"temperature":0.1,"messages":[
 {"role":"user","content":[
   {"type":"text","text":"List the shapes, their colors, and any number shown."},
   {"type":"image_url","image_url":{"url":"data:image/png;base64,"+b}}]}]}))
PY
    chat /tmp/gxacc_body.json > /tmp/gxacc_miniv.json
    local v; v=$(content /tmp/gxacc_miniv.json | tr 'A-Z' 'a-z')
    if [[ "$v" == __error__* ]]; then fail "gx-mini vision" "${v:9:200}"
    elif [[ "$v" == *red* && "$v" == *blue* && "$v" == *circle* && "$v" == *square* && "$v" == *7* ]]; then
      pass "gx-mini vision (identified red circle, blue square, digit 7)"
    else fail "gx-mini vision" "did not identify all elements: ${v:0:200}"; fi
  else skip "gx-mini vision" "no test image at /tmp/gxacc_vision.png"; fi
}

# ------------------------------------------------------------------ 2. fast
t_fast(){
  echo "[2] gx-fast"
  body gx-fast "What is 17*23? Answer with the number only." 60
  chat /tmp/gxacc_body.json > /tmp/gxacc_fast.json
  local c; c=$(content /tmp/gxacc_fast.json)
  if [[ "$c" == __ERROR__* ]]; then fail "gx-fast text" "${c:9:200}"
  elif [[ "$c" == *391* ]]; then pass "gx-fast text inference (correct arithmetic)"
  else fail "gx-fast text" "expected 391, got: ${c:0:120}"; fi

  cat > /tmp/gxacc_body.json <<'JSON'
{"model":"gx-fast","max_tokens":200,"temperature":0,
 "messages":[{"role":"user","content":"What is the weather in Berlin right now? Use the tool."}],
 "tools":[{"type":"function","function":{"name":"get_weather","description":"Get current weather",
   "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],
 "tool_choice":"auto"}
JSON
  chat /tmp/gxacc_body.json > /tmp/gxacc_fasttool.json
  python3 -c "
import json,sys
d=json.load(open('/tmp/gxacc_fasttool.json'))
tc=d.get('choices',[{}])[0].get('message',{}).get('tool_calls')
sys.exit(0 if tc and tc[0]['function']['name']=='get_weather' else 1)" 2>/dev/null \
    && pass "gx-fast tool calling (parsed get_weather)" \
    || fail "gx-fast tool calling" "no parsed tool_calls"
}

# ---------------------------------------------------------------- 3. reason
t_reason(){
  echo "[3] gx-reason"
  body gx-reason "A train leaves at 14:05 travelling 80 km/h. A second leaves the same station at 14:35 at 120 km/h on the same track. At what clock time does the second catch the first? Show the calculation." 400
  GX_TIMEOUT=1800 chat /tmp/gxacc_body.json > /tmp/gxacc_reason.json
  local c; c=$(content /tmp/gxacc_reason.json)
  if [[ "$c" == __ERROR__* ]]; then fail "gx-reason inference" "${c:9:250}"
  elif [[ "$c" == *15:3* || "$c" == *"15.35"* || "$c" == *"3:35"* ]]; then
    pass "gx-reason hard reasoning (correct answer 15:35)"
  elif [ ${#c} -gt 50 ]; then fail "gx-reason reasoning" "answered but result looks wrong: ${c:0:200}"
  else fail "gx-reason inference" "empty/short: ${c:0:120}"; fi
}

# ------------------------------------------------------------------- 4. max
t_max(){
  echo "[4] gx-max (two-node, SLOW)"
  body gx-max "Explain in three sentences why MoE decode is memory-bandwidth bound." 250
  GX_TIMEOUT=2400 chat /tmp/gxacc_body.json > /tmp/gxacc_max.json
  local c; c=$(content /tmp/gxacc_max.json)
  if [[ "$c" == __ERROR__* ]]; then fail "gx-max inference" "${c:9:250}"; return; fi
  [ ${#c} -gt 40 ] && pass "gx-max inference" || fail "gx-max inference" "empty: ${c:0:120}"

  local r0 r1
  r0=$(docker inspect -f '{{.State.Status}}' gx-max-rank0 2>/dev/null | head -1)
  r1=$(ssh -o BatchMode=yes -o ConnectTimeout=8 legenex-02@gx10-02 \
        "docker inspect -f '{{.State.Status}}' gx-max-rank1 2>/dev/null" 2>/dev/null | head -1)
  [ "$r0" = running ] && [ "$r1" = running ] \
    && pass "gx-max both ranks running (node1=$r0 node2=$r1)" \
    || fail "gx-max ranks" "node1=${r0:-absent} node2=${r1:-absent}"

  # Prove the ConnectX fabric actually carried traffic for this request.
  local before after delta
  before=$(cat /sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data 2>/dev/null || echo 0)
  body gx-max "Count from one to forty in words." 300
  GX_TIMEOUT=600 chat /tmp/gxacc_body.json > /dev/null
  after=$(cat /sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data 2>/dev/null || echo 0)
  delta=$(( (after - before) * 4 / 1000000 ))
  [ "$delta" -gt 10 ] \
    && pass "gx-max NCCL over ConnectX active (${delta} MB RDMA on rail A)" \
    || fail "gx-max fabric" "only ${delta} MB moved on the RoCE rail"
}

# ------------------------------------------------------------------ 5. auto
t_auto(){
  echo "[5] gx-auto routing"
  local log=/srv/logs/gx-orchestrator.log
  declare -A want=(
    ["hi there"]=gx-mini
    ["Classify this ticket as billing, technical, or other: my card was charged twice."]=gx-mini
    ["Debug this stack trace and derive the time complexity, then refactor the algorithm."]=gx-reason
  )
  for prompt in "${!want[@]}"; do
    local expect="${want[$prompt]}"
    local before; before=$(grep -c 'gx.routing' "$log" 2>/dev/null || echo 0)
    body gx-auto "$prompt" 40
    GX_TIMEOUT=1800 chat /tmp/gxacc_body.json > /dev/null
    local got
    got=$(grep 'gx.routing' "$log" 2>/dev/null | tail -1 \
      | python3 -c "import sys,json;print(json.loads(sys.stdin.read().split('gx.routing ',1)[-1])['tier'])" 2>/dev/null)
    [ "$got" = "$expect" ] \
      && pass "gx-auto: '${prompt:0:38}...' -> ${got}" \
      || fail "gx-auto routing" "'${prompt:0:38}...' expected ${expect}, got ${got:-<none>}"
  done
}

# -------------------------------------------------------------- 6. lifecycle
t_lifecycle(){
  echo "[6] gx-max lifecycle (SLOW)"
  "${repo}/legenex/lifecycle/gx-max-start.sh" >/tmp/gxacc_start.log 2>&1
  if [ $? -ne 0 ]; then fail "gx-max acquire" "$(tail -3 /tmp/gxacc_start.log | tr '\n' ' ')"; return; fi
  pass "gx-max acquired both nodes"

  body gx-max "Say READY." 20
  GX_TIMEOUT=600 chat /tmp/gxacc_body.json > /tmp/gxacc_lc.json
  [[ "$(content /tmp/gxacc_lc.json)" != __ERROR__* ]] \
    && pass "gx-max serves after acquire" || fail "gx-max post-acquire" "no answer"

  "${repo}/legenex/lifecycle/gx-max-stop.sh" --grace 20 >/tmp/gxacc_stop.log 2>&1
  local avail; avail=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  [ "$avail" -gt 80 ] \
    && pass "gx-max released, node 1 reclaimed ${avail}GiB" \
    || fail "gx-max release" "only ${avail}GiB available after release"

  curl -fsS -m 10 "${GATEWAY}/health/liveliness" >/dev/null 2>&1 \
    && pass "gateway survived the lifecycle cycle" || fail "gateway after lifecycle" "not responding"
}

# ------------------------------------------------------------- 7/8. media
t_media(){
  echo "[7/8] gx-image / gx-video"
  local media_key
  if [ -f "${ENV_FILE}" ]; then
    media_key="$(grep '^GX_MEDIA_API_KEY=' "${ENV_FILE}" | cut -d= -f2-)"
  else
    media_key="${GX_MEDIA_API_KEY:-}"
  fi
  if [ -z "${media_key}" ]; then
    skip "gx-image generation" "no GX_MEDIA_API_KEY found"
    skip "gx-video generation" "no GX_MEDIA_API_KEY found"
    return
  fi

  local health; health=$(curl -fsS -m 10 http://192.168.100.11:18800/health 2>&1)
  if [[ "$(printf '%s' "${health}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("comfyui",{}).get("reachable"))' 2>/dev/null)" != "True" ]]; then
    skip "gx-image generation" "media router/ComfyUI not reachable -- not deployed this run"
    skip "gx-video generation" "media router/ComfyUI not reachable -- not deployed this run"
    return
  fi

  # Real image generation through the gateway (the single ingress a real
  # client uses), not the router directly -- proves the whole production path.
  local img_resp
  img_resp=$(curl -sS -m 60 -X POST "${GATEWAY}/v1/images/generations" \
    -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
    -d '{"model":"gx-image","prompt":"a single red apple on a wooden table, soft daylight","size":"1024x1024","n":1}')
  local img_b64; img_b64=$(printf '%s' "${img_resp}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(d["data"][0].get("b64_json") or d["data"][0].get("url") or "")
except Exception: print("")' 2>/dev/null)
  [ -n "${img_b64}" ] \
    && pass "gx-image real generation via gateway" \
    || fail "gx-image generation" "${img_resp:0:200}"

  # Real video generation, async: submit -> poll -> fetch content. Router is
  # the documented client contract for /v1/videos (README.md), so this hits
  # it directly rather than guessing at a LiteLLM video pass-through shape.
  local vid_id
  vid_id=$(curl -sS -m 15 -X POST http://192.168.100.11:18800/v1/videos \
    -H "Authorization: Bearer ${media_key}" -H 'Content-Type: application/json' \
    -d '{"prompt":"a candle flame flickering gently in still air","seconds":2}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
  if [ -z "${vid_id}" ]; then
    fail "gx-video submit" "no job id returned"
  else
    local vid_status="" i
    for i in $(seq 1 60); do
      vid_status=$(curl -sS -m 10 "http://192.168.100.11:18800/v1/videos/${vid_id}" \
        -H "Authorization: Bearer ${media_key}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
      [ "${vid_status}" = "completed" ] || [ "${vid_status}" = "failed" ] && break
      sleep 5
    done
    if [ "${vid_status}" = "completed" ]; then
      local vid_bytes
      vid_bytes=$(curl -sS -m 30 "http://192.168.100.11:18800/v1/videos/${vid_id}/content" \
        -H "Authorization: Bearer ${media_key}" -o /tmp/gxacc_video.mp4 -w '%{size_download}')
      [ "${vid_bytes:-0}" -gt 1000 ] \
        && pass "gx-video real generation, ${vid_bytes} bytes" \
        || fail "gx-video content fetch" "only ${vid_bytes:-0} bytes"
    else
      fail "gx-video generation" "status=${vid_status:-timeout}"
    fi
  fi
}

# ----------------------------------------------------------- 9. restart test
t_restart(){
  echo "[9] gateway restart recovery"
  ( cd "${repo}/legenex/gateway" && docker compose --env-file .env -f docker-compose.gateway.yml restart ) >/dev/null 2>&1
  local ok=0
  for _ in $(seq 1 60); do
    curl -fsS -m 3 "${GATEWAY}/health/liveliness" >/dev/null 2>&1 && { ok=1; break; }
    sleep 2
  done
  [ "$ok" = 1 ] && pass "gateway recovered after restart" || { fail "gateway restart" "did not come back"; return; }
  body gx-mini "Say OK." 20
  chat /tmp/gxacc_body.json > /tmp/gxacc_rs.json
  [[ "$(content /tmp/gxacc_rs.json)" != __ERROR__* ]] \
    && pass "aliases serve again after restart" || fail "post-restart inference" "gx-mini failed"
}

# -------------------------------------------------------------------- driver
ALL=(gateway mini fast reason auto media restart)
SLOW=(max lifecycle)
if [ $# -gt 0 ]; then
  SELECTED=("$@")
else
  SELECTED=("${ALL[@]}")
  [ "${GX_RUN_SLOW:-0}" = "1" ] && SELECTED+=("${SLOW[@]}")
fi

echo "=============================================="
echo " gx-cluster acceptance suite  $(date -Is)"
echo " gateway=${GATEWAY}"
echo "=============================================="
for t in "${SELECTED[@]}"; do
  if declare -F "t_${t}" >/dev/null; then "t_${t}"; else echo "  (no such test: ${t})"; fi
done

echo
echo "=============================================="
printf ' PASS=%d  FAIL=%d  SKIP=%d\n' "$PASS" "$FAIL" "$SKIP"
echo "=============================================="
printf '%s\n' "${RESULTS[@]}"
exit "$FAIL"
