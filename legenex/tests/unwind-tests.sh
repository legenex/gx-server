#!/usr/bin/env bash
# ============================================================================
# unwind-tests.sh — regression tests for the gx-max failure-unwind machinery.
#
# These exercise the REAL scripts against REAL containers, but never the real
# model: every test uses a placeholder image, so the whole suite runs in about
# a minute and costs no memory. What it proves is the logic that decides
# whether a rank lives or dies -- the logic B-020 turned out not to have.
#
# Coverage:
#   E1  deadman fires when rank0 never appears           (startup grace)
#   E2  deadman arms on rank0, fires when rank0 vanishes (death grace)
#   E3  deadman fires on its own memory floor
#   E4  deadman does NOT apply the steady floor before the engine is ready,
#       and DOES apply it after                          (phase latch)
#   E5  gx-max-start.sh unwinds both nodes on a failed launch, and the
#       unwind reports a verified-clean cluster
#
# E5 IS DESTRUCTIVE. It runs the real gx-max-start.sh, which drains gx-mini,
# gx-fast, gx-reason, ComfyUI, the media router and both llama-swaps before it
# does anything else -- that is the behaviour under test. Do NOT run it
# concurrently with the acceptance suite or with live traffic: doing so on
# 2026-09-16 SIGKILLed a gx-reason that an acceptance run was mid-way through
# loading, and the resulting "gx-reason inference failed" looked like a
# product bug for several minutes. Run `unwind-tests.sh E1 E2 E3 E4` for the
# non-destructive subset.
#
# Usage: legenex/tests/unwind-tests.sh [E1 E2 ...]
# Exit code is the number of failures.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LC="${here}/../lifecycle"
IMAGE="${GX_TEST_IMAGE:-python:3.12-slim}"
PASS=0; FAIL=0
pass(){ PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail(){ FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n        %s\n' "$1" "$2"; }

DM_LOG="${HOME}/gx-max-rank1-deadman.log"
PIDF="${HOME}/.gx-guard/rank1-deadman.pid"

# A unique marker embedded in every helper process this suite starts, so they
# can be found and killed by pattern without matching anything else on the
# host. Needed because an interrupted run left a fake "engine ready" HTTP
# server bound to its port -- and the NEXT run's E4 then saw a healthy engine
# it had not started, latched to the steady floor immediately, and reported a
# premature fire. A suite that can be fooled by its own leftovers is worse
# than no suite.
HELPER_TAG="gx-unwind-test-helper"

kill_helpers(){ pkill -f "${HELPER_TAG}" >/dev/null 2>&1 || true; }
# Per-test cleanup: the deadman under test and its container. It deliberately
# does NOT touch the helper processes -- `arm()` calls this, and E2 starts its
# fake rank0 BEFORE arming, so killing helpers here would kill the very
# listener the test needs the deadman to see ("E2 arm: never saw rank0").
cleanup_dm(){
  [ -f "${PIDF}" ] && kill "$(cat "${PIDF}" 2>/dev/null)" 2>/dev/null
  rm -f "${PIDF}"
  docker rm -f dm-test >/dev/null 2>&1
}
# Suite-level cleanup: everything, including helpers.
cleanup_all(){ cleanup_dm; kill_helpers; }
trap cleanup_all EXIT
kill_helpers   # clear anything a previous, interrupted run left behind

arm(){ # arm <dist_addr> <startup_grace> <death_grace> <steady_floor> <load_floor> <health_url>
  cleanup_dm; rm -f "${DM_LOG}"
  docker run -d --name dm-test "${IMAGE}" sleep 300 >/dev/null
  setsid nohup "${LC}/rank1-deadman.sh" "$1" dm-test "$2" "$3" 2 "$4" /tmp/dmtest.tsv "$5" "$6" \
    >/dev/null 2>&1 </dev/null &
  sleep 3
}
gone(){ [ -z "$(docker inspect -f '{{.State.Running}}' dm-test 2>/dev/null | tr -d '[:space:]')" ]; }
wait_gone(){ local n="${1:-20}"; for _ in $(seq 1 "$n"); do gone && return 0; sleep 1; done; return 1; }

# A TCP listener that stands in for rank0's bootstrap store.
#
# NOTE the `>/dev/null 2>&1` on both background helpers below: a backgrounded
# child inherits the command substitution's stdout, so `pid=$(cmd & echo $!)`
# blocks until that child exits -- which, for a server, is never. Found
# 2026-09-16 when E2/E4 hung instead of failing.
fake_rank0(){ python3 -c "
# gx-unwind-test-helper
import socket,sys,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',$1)); s.listen(8); s.settimeout(1)
t=time.time()
while time.time()-t < $2:
    try: c,_=s.accept(); c.close()
    except Exception: pass
s.close()" >/dev/null 2>&1 & echo $!; }

t_E1(){
  echo "[E1] deadman fires when rank0 never appears"
  kill_helpers
  arm 127.0.0.1:39998 8 60 0 0 http://127.0.0.1:1/health
  wait_gone 30 && pass "E1 orphan removed after the startup grace" \
                || fail "E1" "container still running after startup grace"
}
t_E2(){
  echo "[E2] deadman arms on rank0, fires when rank0 vanishes"
  local pid; pid=$(fake_rank0 39997 10)
  arm 127.0.0.1:39997 600 6 0 0 http://127.0.0.1:1/health
  grep -q "now armed" "${DM_LOG}" 2>/dev/null \
    && pass "E2 armed while rank0 was alive" || fail "E2 arm" "never saw rank0"
  wait_gone 40 && pass "E2 orphan removed after rank0 vanished" \
                || fail "E2" "container survived rank0's disappearance"
  kill "${pid}" 2>/dev/null || true
}
t_E3(){
  echo "[E3] deadman fires on its own load-phase memory floor"
  kill_helpers
  arm 127.0.0.1:39996 600 60 0 999 http://127.0.0.1:1/health
  wait_gone 20 && pass "E3 removed on the load-phase floor" \
                || fail "E3" "memory floor did not fire"
}
t_E4(){
  echo "[E4] phase latch: steady floor applies only once the engine is ready"
  kill_helpers
  # load floor disabled, steady floor impossible: must NOT fire while the
  # health endpoint is down, MUST fire once it answers.
  # Fail loudly rather than silently mis-test if the port is already taken.
  if timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/39995' 2>/dev/null; then
    fail "E4 precondition" "something is already listening on 127.0.0.1:39995"
    return
  fi
  arm 127.0.0.1:39995 600 60 999 0 http://127.0.0.1:39995/health
  sleep 6
  gone && { fail "E4 premature" "fired before the engine was ready"; return; }
  pass "E4 held during the load phase (steady floor not applied)"
  local pid; pid=$(python3 -c "
# ${HELPER_TAG}
import http.server,socketserver
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(s): s.send_response(200); s.end_headers(); s.wfile.write(b'ok')
    def log_message(*a): pass
socketserver.TCPServer(('127.0.0.1',39995),H).serve_forever()" >/dev/null 2>&1 & echo $!)
  wait_gone 25 && pass "E4 fired once the engine answered /health" \
                || fail "E4 latch" "steady floor never applied after readiness"
  kill "${pid}" 2>/dev/null || true
}
t_E5(){
  echo "[E5] gx-max-start.sh unwinds both nodes on a failed launch"
  kill_helpers
  local log; log=$(mktemp)
  GXMAX_IMAGE="${IMAGE}" GXMAX_RANK_ESTIMATED_GIB=2 GXMAX_READY_TIMEOUT=60 \
  GXMAX_DEADMAN_STARTUP_GRACE=120 GXMAX_DEADMAN_DEATH_GRACE=20 GXMAX_DEADMAN_POLL=5 \
  GXMAX_SENTINEL_POLL=5 GXMAX_SHM_BYTES=1073741824 GXMAX_MEM_LIMIT=2g \
  GXMAX_LOAD_FLOOR_GIB=0 GXMAX_ABORT_FLOOR_GIB=0 \
    bash "${LC}/gx-max-start.sh" >"${log}" 2>&1
  local rc=$?
  [ "${rc}" -ne 0 ] && pass "E5 failed launch reported a non-zero exit (${rc})" \
                    || fail "E5 exit" "a launch that cannot work returned 0"
  grep -q "UNWIND COMPLETE — cluster verified clean" "${log}" \
    && pass "E5 unwind reported a verified-clean cluster" \
    || fail "E5 unwind" "$(grep -E 'unwind: FAIL' "${log}" | head -3 | tr '\n' ' ')"
  grep -q "unwind: ok   rank0 confirmed not running" "${log}" \
    && pass "E5 rank0 confirmed gone" || fail "E5 rank0" "not confirmed"
  grep -q "unwind: ok   rank1 confirmed not running" "${log}" \
    && pass "E5 rank1 confirmed gone" || fail "E5 rank1" "not confirmed"
  rm -f "${log}"
}

ALL=(E1 E2 E3 E4 E5)
SEL=("$@"); [ $# -eq 0 ] && SEL=("${ALL[@]}")
echo "=== gx-max unwind regression tests  $(date -Is) ==="
# if/else, not `A && B || C`: with the short-circuit form, a test function
# that merely RETURNS non-zero (t_E2 ends on a `kill` of an already-exited
# process) falls through to the "no such test" branch and prints a confusing
# message after the test has actually run and passed.
for t in "${SEL[@]}"; do
  if declare -F "t_${t}" >/dev/null; then
    "t_${t}" || true
  else
    echo "  (no such test: ${t})"
  fi
done
printf '\n PASS=%d  FAIL=%d\n' "${PASS}" "${FAIL}"
exit "${FAIL}"
