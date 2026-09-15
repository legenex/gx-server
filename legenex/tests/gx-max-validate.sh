#!/usr/bin/env bash
# ==============================================================================
# gx-max-validate.sh — full acquire -> health-check-both-ranks -> serve ->
# release -> restore-normal cycle for gx-max, driven through the REAL
# orchestrator HTTP API (the same code path a client's `model: gx-max` request
# takes in production), not by calling the lifecycle shell scripts directly.
#
# Run this ONLY when both nodes are confirmed healthy -- e.g. after
# legenex/scripts/recover-node2.sh reports all PASS. It is NOT a dry run: it
# really starts the two-node SGLang engine, which evicts gx-mini/gx-fast on
# node 1 and gx-reason/ComfyUI on node 2 for the duration of the test.
#
# Why through the orchestrator and not gx-max-start.sh directly: the whole
# point of legenex/orchestrator/gx_orchestrator/lifecycle.py is to be the one
# place that serialises acquisition and enforces "never silently downgrade".
# Calling the shell scripts directly (as legenex/tests/acceptance.sh's slow
# suite does) validates the scripts; calling the HTTP API additionally
# validates the state machine itself -- the acquire/release/reconcile logic,
# and the exact behaviour a real gx-max client request gets.
#
# What it checks, in order:
#   1.  Preflight: kernel on both nodes, ConnectX rails up, SSH to node2,
#       orchestrator reachable, gx-max not unexpectedly already mid-cycle.
#   2.  POST /lifecycle/gx-max/acquire -- times it, and treats a clean 503
#       ("could not be brought up, will not be substituted") as a well-formed
#       FAIL rather than a crash: that is the architecture's documented
#       failure mode (ARCHITECTURE.md section 5, "never-downgrade rule").
#   3.  Health-check BOTH ranks independently: docker state on node1, docker
#       state on node2 (over SSH), the orchestrator's own status view, and
#       the engine's own /health -- four independent signals, not just one.
#   4.  Serve: a real completion through the full gateway path, plus proof
#       (RDMA counter delta) that it actually crossed the ConnectX fabric,
#       not just node1's local rank0 answering from cache.
#   5.  POST /lifecycle/gx-max/release -- graceful drain, default grace.
#   6.  Verify restore-normal actually happened: gateway alive, orchestrator
#       alive, both ranks GONE (not just "released" in the state machine),
#       memory reclaimed on both nodes.
#
# Known safety gap this script exists partly to make visible (see the lead's
# report on Task 9 / lifecycle.py): if acquisition fails or times out AFTER
# rank1 (node2) already started but before rank0 (node1) becomes healthy,
# nothing in _do_acquire() currently tears rank0 back down automatically. If
# this script's acquire step fails, it explicitly re-checks `docker ps` on
# BOTH nodes afterward and calls that fact out loudly rather than silently
# moving on -- do not ignore that warning if it fires.
#
# This script performs NO cleanup-by-force by default: on a failure it leaves
# the cluster in whatever state the failure produced and tells you exactly
# what it saw, so a human can diagnose the real fault instead of the script
# papering over it. Use --cleanup-on-exit to opt into an automatic best-effort
# release when you specifically want the cluster left idle no matter what.
#
# Usage:
#   legenex/tests/gx-max-validate.sh                  # full cycle, default
#   legenex/tests/gx-max-validate.sh --cleanup-on-exit
#   legenex/tests/gx-max-validate.sh --skip-preflight  # if you already know
#
# Exit code = number of FAILed checks (0 = full cycle passed).
# ==============================================================================
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ORCH="${GX_ORCH:-http://127.0.0.1:18900}"
GATEWAY="${GX_GATEWAY:-http://127.0.0.1:4000}"
NODE2_SSH="${GX_NODE2_SSH:-legenex-02@gx10-02}"
NODE2_FABRIC_A="${GX_NODE2_FABRIC_A:-192.168.100.11}"
NODE2_FABRIC_B="${GX_NODE2_FABRIC_B:-192.168.101.11}"
EXPECTED_KERNEL="${GX_EXPECTED_KERNEL:-6.17.0-1032-nvidia}"
SSH_CONNECT_TIMEOUT="${GX_SSH_CONNECT_TIMEOUT:-10}"
SSH_HARD_TIMEOUT="${GX_SSH_HARD_TIMEOUT:-20}"

ACQUIRE_TIMEOUT="${GX_MAX_VALIDATE_ACQUIRE_TIMEOUT:-1800}"
RELEASE_TIMEOUT="${GX_MAX_VALIDATE_RELEASE_TIMEOUT:-900}"
SERVE_TIMEOUT="${GX_MAX_VALIDATE_SERVE_TIMEOUT:-600}"

CLEANUP_ON_EXIT=0
SKIP_PREFLIGHT=0

ENV_FILE="${repo}/legenex/gateway/.env"
if [ -f "${ENV_FILE}" ]; then
  GATEWAY_KEY="$(grep '^LITELLM_MASTER_KEY=' "${ENV_FILE}" | cut -d= -f2-)"
else
  GATEWAY_KEY="${LITELLM_MASTER_KEY:-}"
fi

usage() {
  sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --cleanup-on-exit) CLEANUP_ON_EXIT=1 ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

# ------------------------------------------------------------------ logging --
PASS=0; FAIL=0; WARN=0
declare -a RESULTS=()
ts() { date -Is; }
log()      { printf '[%s] %s\n' "$(ts)" "$*" >&2; }
step_pass(){ PASS=$((PASS+1)); log "PASS  $1"; RESULTS+=("PASS  $1"); }
step_fail(){ FAIL=$((FAIL+1)); log "FAIL  $1 :: ${2:-}"; RESULTS+=("FAIL  $1 :: ${2:-}"); }
step_warn(){ WARN=$((WARN+1)); log "WARN  $1 :: ${2:-}"; RESULTS+=("WARN  $1 :: ${2:-}"); }

remote() {
  timeout "${SSH_HARD_TIMEOUT}" ssh -o BatchMode=yes \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "${NODE2_SSH}" "$@"
}

cleanup_trap() {
  if [ "${CLEANUP_ON_EXIT}" -eq 1 ]; then
    log "=== --cleanup-on-exit: releasing gx-max (best effort) ==="
    curl -fsS -m "${RELEASE_TIMEOUT}" -X POST "${ORCH}/lifecycle/gx-max/release" \
      -H 'Content-Type: application/json' -d '{"force": true, "restore": true}' >/dev/null 2>&1 || true
  fi
}
trap cleanup_trap EXIT

json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

# ------------------------------------------------------------------ preflight
preflight() {
  log "=== preflight ==="
  if [ "${SKIP_PREFLIGHT}" -eq 1 ]; then
    step_warn "preflight" "skipped by --skip-preflight; proceeding on faith"
    return
  fi

  local k1 k2
  k1=$(uname -r)
  [ "${k1}" = "${EXPECTED_KERNEL}" ] \
    && step_pass "node1 kernel is ${EXPECTED_KERNEL}" \
    || { step_fail "node1 kernel" "got '${k1}', expected ${EXPECTED_KERNEL} (L-4)"; return 1; }

  if ! remote true 2>/dev/null; then
    step_fail "node2 ssh" "not reachable -- do not proceed. Run legenex/scripts/recover-node2.sh first."
    return 1
  fi
  step_pass "node2 ssh reachable"

  k2=$(remote uname -r 2>/dev/null)
  [ "${k2}" = "${EXPECTED_KERNEL}" ] \
    && step_pass "node2 kernel is ${EXPECTED_KERNEL}" \
    || { step_fail "node2 kernel" "got '${k2}', expected ${EXPECTED_KERNEL} (L-4)"; return 1; }

  # Not `ping`: needs CAP_NET_RAW, which fails under a NoNewPrivileges=true
  # systemd unit even with the binary's file capability set, and this host's
  # ping_group_range has no unprivileged fallback either -- see the matching
  # fix and full explanation in gx-max-start.sh. A plain TCP connect attempt
  # needs no special capability; a fast "Connection refused" proves the peer
  # answered (nothing needs to listen on the probe port), a real timeout
  # means genuinely unreachable.
  fabric_rail_reachable() {
    local peer="$1" out rc
    out=$(timeout 2 bash -c "exec 3<>/dev/tcp/${peer}/1" 2>&1); rc=$?
    [ "${rc}" -eq 0 ] && return 0
    printf '%s' "${out}" | grep -qi 'connection refused'
  }
  for peer in "${NODE2_FABRIC_A}" "${NODE2_FABRIC_B}"; do
    fabric_rail_reachable "${peer}" \
      && step_pass "fabric rail reachable: ${peer}" \
      || { step_fail "fabric rail" "${peer} unreachable"; return 1; }
  done

  curl -fsS -m 10 "${ORCH}/health" >/dev/null 2>&1 \
    && step_pass "orchestrator reachable at ${ORCH}" \
    || { step_fail "orchestrator" "not reachable at ${ORCH}/health"; return 1; }

  local state
  state=$(curl -fsS -m 10 "${ORCH}/lifecycle/gx-max/status" 2>/dev/null | json_get "['state']")
  case "${state}" in
    down|"") step_pass "gx-max is DOWN before this run starts (clean baseline)" ;;
    ready)   step_warn "gx-max already READY" "it will simply be adopted; the acquire step below should return immediately" ;;
    acquiring|releasing)
      step_fail "gx-max mid-cycle" "state=${state} -- another operation is already in flight. Do not start a second one; wait for it to settle first."
      return 1
      ;;
    *) step_warn "gx-max status" "unrecognised state '${state}'" ;;
  esac
  return 0
}

# --------------------------------------------------------------------- acquire
ACQUIRE_OK=0

do_acquire() {
  log "=== acquire (POST ${ORCH}/lifecycle/gx-max/acquire) ==="
  local start end elapsed resp http_code body
  start=$(date +%s)
  resp=$(curl -sS -m "$((ACQUIRE_TIMEOUT + 30))" -w '\n__HTTP__%{http_code}' \
    -X POST "${ORCH}/lifecycle/gx-max/acquire" \
    -H 'Content-Type: application/json' \
    -d "{\"timeout\": ${ACQUIRE_TIMEOUT}}" 2>&1)
  end=$(date +%s); elapsed=$((end - start))
  http_code=$(printf '%s' "${resp}" | grep -o '__HTTP__[0-9]*' | tail -1 | sed 's/__HTTP__//')
  body=$(printf '%s' "${resp}" | sed 's/__HTTP__[0-9]*$//')

  if [ "${http_code}" = "200" ]; then
    step_pass "gx-max acquired in ${elapsed}s"
    ACQUIRE_OK=1
    return 0
  fi

  if [ "${http_code}" = "503" ]; then
    step_fail "gx-max acquisition" "orchestrator correctly refused rather than downgrading (${elapsed}s): $(printf '%s' "${body}" | head -c 300)"
  else
    step_fail "gx-max acquisition" "unexpected http_code='${http_code}' after ${elapsed}s: $(printf '%s' "${body}" | head -c 300)"
  fi

  # This is the partial-failure question from Task 9: did rank0 get left
  # running alone on node1 while rank1 never came up (or vice versa)? Check
  # explicitly and shout if so -- do not let this go unnoticed.
  local r0 r1
  r0=$(docker inspect -f '{{.State.Status}}' gx-max-rank0 2>/dev/null || echo absent)
  r1=$(remote "docker inspect -f '{{.State.Status}}' gx-max-rank1 2>/dev/null" 2>/dev/null || echo absent)
  if [ "${r0}" = "running" ] && [ "${r1}" != "running" ]; then
    step_fail "orphaned rank0" "*** rank0 is RUNNING on node1 (burning ~80GB) while rank1 is '${r1}' on node2. This is the exact partial-failure gap: gx_orchestrator/lifecycle.py's _do_acquire() does not automatically tear down a partially-started acquisition. Run: legenex/lifecycle/gx-max-stop.sh --force --no-restore ; docker rm -f gx-max-rank0 -- then investigate node2 before retrying."
  elif [ "${r1}" = "running" ] && [ "${r0}" != "running" ]; then
    step_warn "orphaned rank1" "rank1 is running on node2 while rank0 is '${r0}' on node1 -- also clean up before retrying: ssh legenex-02@gx10-02 'docker rm -f gx-max-rank1'"
  fi
  return 1
}

# --------------------------------------------------------------- health-check
health_check_both_ranks() {
  log "=== health-check both ranks ==="
  local r0 r1 orch_state engine_health
  r0=$(docker inspect -f '{{.State.Status}}' gx-max-rank0 2>/dev/null || echo absent)
  [ "${r0}" = "running" ] \
    && step_pass "node1 rank0 container running" \
    || step_fail "node1 rank0 container" "status=${r0}"

  r1=$(remote "docker inspect -f '{{.State.Status}}' gx-max-rank1 2>/dev/null" 2>/dev/null || echo absent)
  [ "${r1}" = "running" ] \
    && step_pass "node2 rank1 container running" \
    || step_fail "node2 rank1 container" "status=${r1}"

  orch_state=$(curl -fsS -m 10 "${ORCH}/lifecycle/gx-max/status" 2>/dev/null | json_get "['state']")
  [ "${orch_state}" = "ready" ] \
    && step_pass "orchestrator reports state=ready" \
    || step_fail "orchestrator state" "expected ready, got '${orch_state}'"

  curl -fsS -m 10 "http://127.0.0.1:30000/health" >/dev/null 2>&1 \
    && step_pass "engine /health OK on :30000" \
    || step_fail "engine health" "no 200 from http://127.0.0.1:30000/health"
}

# ------------------------------------------------------------------------ serve
serve_and_verify_fabric() {
  log "=== serve (real inference through the gateway) ==="
  if [ -z "${GATEWAY_KEY}" ]; then
    step_warn "serve" "no LITELLM_MASTER_KEY found (checked ${ENV_FILE} and env); skipping the gateway request"
    return
  fi

  local body resp content
  body=$(python3 -c 'import json,sys;print(json.dumps({"model":"gx-max","max_tokens":40,"temperature":0,"messages":[{"role":"user","content":"Say READY and nothing else."}]}))')
  resp=$(curl -sS -m "${SERVE_TIMEOUT}" "${GATEWAY}/v1/chat/completions" \
    -H "Authorization: Bearer ${GATEWAY_KEY}" -H 'Content-Type: application/json' \
    --data-binary "${body}")
  content=$(printf '%s' "${resp}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if 'error' in d: print('__ERROR__'+json.dumps(d['error'])[:300])
else: print((d['choices'][0]['message'].get('content') or '').strip())
" 2>/dev/null)

  if [[ "${content}" == __ERROR__* ]]; then
    step_fail "gx-max inference" "${content:9:250}"
    return
  fi
  [ -n "${content}" ] \
    && step_pass "gx-max served a real completion: '${content:0:80}'" \
    || step_fail "gx-max inference" "empty response"

  # Prove the ConnectX fabric actually carried traffic for this request,
  # not just rank0 answering from a warm local cache.
  local before after delta
  before=$(cat /sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data 2>/dev/null || echo 0)
  body=$(python3 -c 'import json;print(json.dumps({"model":"gx-max","max_tokens":150,"temperature":0,"messages":[{"role":"user","content":"Count from one to thirty in words."}]}))')
  curl -sS -m "${SERVE_TIMEOUT}" "${GATEWAY}/v1/chat/completions" \
    -H "Authorization: Bearer ${GATEWAY_KEY}" -H 'Content-Type: application/json' \
    --data-binary "${body}" >/dev/null
  after=$(cat /sys/class/infiniband/rocep1s0f0/ports/1/counters/port_xmit_data 2>/dev/null || echo 0)
  delta=$(( (after - before) * 4 / 1000000 ))
  [ "${delta}" -gt 5 ] \
    && step_pass "RDMA traffic observed on rail A (${delta} MB) -- genuinely a two-node job" \
    || step_warn "RDMA traffic" "only ${delta} MB moved on rail A; the fabric may not have been exercised as expected"
}

# ----------------------------------------------------------------------- release
do_release() {
  log "=== release (POST ${ORCH}/lifecycle/gx-max/release) ==="
  local resp http_code body
  resp=$(curl -sS -m "$((RELEASE_TIMEOUT + 30))" -w '\n__HTTP__%{http_code}' \
    -X POST "${ORCH}/lifecycle/gx-max/release" \
    -H 'Content-Type: application/json' -d '{"force": false, "restore": true}' 2>&1)
  http_code=$(printf '%s' "${resp}" | grep -o '__HTTP__[0-9]*' | tail -1 | sed 's/__HTTP__//')
  body=$(printf '%s' "${resp}" | sed 's/__HTTP__[0-9]*$//')
  [ "${http_code}" = "200" ] \
    && step_pass "release accepted" \
    || step_fail "release" "http_code='${http_code}': $(printf '%s' "${body}" | head -c 300)"
}

# ------------------------------------------------------------- restore-normal
verify_restore_normal() {
  log "=== verify restore-normal ==="
  sleep 5

  local state
  state=$(curl -fsS -m 10 "${ORCH}/lifecycle/gx-max/status" 2>/dev/null | json_get "['state']")
  [ "${state}" = "down" ] \
    && step_pass "orchestrator reports state=down after release" \
    || step_fail "orchestrator state after release" "expected down, got '${state}'"

  local r0 r1
  r0=$(docker inspect -f '{{.State.Status}}' gx-max-rank0 2>/dev/null || echo absent)
  [ "${r0}" = "absent" ] \
    && step_pass "rank0 container fully torn down on node1" \
    || step_fail "rank0 teardown" "still present, status=${r0}"

  r1=$(remote "docker inspect -f '{{.State.Status}}' gx-max-rank1 2>/dev/null" 2>/dev/null || echo absent)
  [ "${r1}" = "absent" ] \
    && step_pass "rank1 container fully torn down on node2" \
    || step_fail "rank1 teardown" "still present, status=${r1}"

  curl -fsS -m 10 "${GATEWAY}/health/liveliness" >/dev/null 2>&1 \
    && step_pass "gateway alive after release" \
    || step_fail "gateway after release" "not responding on ${GATEWAY}"

  curl -fsS -m 10 "${ORCH}/health" >/dev/null 2>&1 \
    && step_pass "orchestrator alive after release" \
    || step_fail "orchestrator after release" "not responding"

  local avail1 avail2
  avail1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  avail2=$(remote awk "'/MemAvailable/{print int(\$2/1048576)}'" /proc/meminfo 2>/dev/null)
  [ "${avail1:-0}" -gt 80 ] \
    && step_pass "node1 memory reclaimed (${avail1} GiB available)" \
    || step_fail "node1 memory" "only ${avail1:-0} GiB available after release"
  [ "${avail2:-0}" -gt 80 ] \
    && step_pass "node2 memory reclaimed (${avail2} GiB available)" \
    || step_fail "node2 memory" "only ${avail2:-0} GiB available after release"

  curl -fsS -m 5 "http://${NODE2_FABRIC_A}:28080/health" >/dev/null 2>&1 \
    && step_pass "node2 llama-swap answered on the fabric (restore-normal's node-2 leg ran)" \
    || step_warn "node2 llama-swap" "not answering yet -- restore-normal.sh's node-2 step is best-effort and may need the compose stack started manually (see RECOVERY.md section 4)"
}

# -------------------------------------------------------------------- driver
echo "=============================================="
echo " gx-max-validate.sh  $(date -Is)"
echo " orchestrator=${ORCH}  gateway=${GATEWAY}"
echo "=============================================="

if preflight; then
  if do_acquire; then
    health_check_both_ranks
    serve_and_verify_fabric
    do_release
    verify_restore_normal
  else
    log "acquire failed -- skipping health/serve/release/restore checks (nothing to check)"
  fi
else
  log "preflight failed -- refusing to attempt acquire"
fi

echo
echo "=============================================="
printf ' PASS=%d  FAIL=%d  WARN=%d\n' "${PASS}" "${FAIL}" "${WARN}"
echo "=============================================="
printf '%s\n' "${RESULTS[@]}"
exit "${FAIL}"
