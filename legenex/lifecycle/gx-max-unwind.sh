#!/usr/bin/env bash
# ============================================================================
# gx-max-unwind.sh — the FAILURE path for a two-node gx-max launch.
#
# gx-max-stop.sh is the *graceful* teardown of a healthy engine. This is the
# other one: something went wrong (rank died, OOM, readiness timeout, operator
# abort) and the cluster must be returned to a known-good state with no orphan
# rank left holding ~80-90 GiB of a node.
#
# It is deliberately separate from gx-max-stop.sh because the requirements are
# different. A graceful stop may assume both nodes are healthy and may take its
# time draining. An unwind must assume the opposite: that the node it needs to
# reach is the node that is currently being starved (that is exactly what
# happened in B-020 -- the unwind's single `ssh` to node 2 timed out because
# the orphan rank1 it was trying to kill had already starved node 2's
# userspace, and the orphan then held the node for 80 minutes).
#
# So this script:
#   * never drains, never waits for in-flight work;
#   * retries the node-2 path with a bounded backoff instead of giving up on
#     one timed-out ssh;
#   * treats the node-2-side deadman (rank1-deadman.sh) as the primary
#     defence and itself as the fast path, not the only path;
#   * CONFIRMS both ranks are actually gone rather than assuming a stop
#     command that returned 0 did anything;
#   * reconciles the residency ledger on BOTH nodes and leaves no held lock;
#   * verifies memory actually came back, swap stopped growing, and
#     SSH/Tailscale still work -- and says so, per node, in its output.
#
# Exit codes: 0 = cluster verified clean | 1 = a rank could not be confirmed
# gone (the one case that needs a human). Everything else is reported as a
# WARN and does not mask the primary result.
#
# Usage: gx-max-unwind.sh [--reason "text"] [--no-restore]
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"

REASON="unspecified"; RESTORE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --reason) REASON="${2:-unspecified}"; shift ;;
    --no-restore) RESTORE=0 ;;
    *) log "unwind: ignoring unknown option $1" ;;
  esac
  shift
done

FAILED=0
warn() { log "unwind: WARN $*"; }
fail() { log "unwind: FAIL $*"; FAILED=1; }
ok()   { log "unwind: ok   $*"; }

# Retries for anything that must reach node 2. B-020's whole failure was a
# single ssh attempt against a node that was 30 seconds from being usable
# again. Backoff: 0, 10, 20, 40, 60, 60 ... capped.
N2_TRIES="${GXMAX_UNWIND_N2_TRIES:-6}"
n2_retry() {
  local i delay=10 out rc
  for (( i=1; i<=N2_TRIES; i++ )); do
    out="$(ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=5 \
            -o ServerAliveCountMax=3 "${GXMAX_NODE2_SSH}" "$@" 2>&1)"; rc=$?
    if [ "${rc}" -eq 0 ]; then printf '%s' "${out}"; return 0; fi
    [ "${i}" -lt "${N2_TRIES}" ] && { log "unwind: node2 attempt ${i}/${N2_TRIES} failed (rc=${rc}); retrying in ${delay}s"; sleep "${delay}"; delay=$(( delay*2 > 60 ? 60 : delay*2 )); }
  done
  printf '%s' "${out}"
  return 1
}

log "================= gx-max UNWIND (reason: ${REASON}) ================="

swap_used_n1_before=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)
swap_used_n2_before=$(n2_retry "awk '/SwapTotal/{t=\$2} /SwapFree/{f=\$2} END{print int((t-f)/1024)}' /proc/meminfo" || echo "")

# ------------------------------------------------------------ 1. kill rank0 --
log "unwind: stopping rank0 on node1"
docker stop -t 20 "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true
docker kill "${GXMAX_RANK0_NAME}"       >/dev/null 2>&1 || true
docker rm -f "${GXMAX_RANK0_NAME}"      >/dev/null 2>&1 || true

# ------------------------------------------------------------ 2. kill rank1 --
log "unwind: stopping rank1 on node2 (bounded retry, ${N2_TRIES} attempts)"
n2_retry "docker stop -t 20 ${GXMAX_RANK1_NAME} >/dev/null 2>&1; docker kill ${GXMAX_RANK1_NAME} >/dev/null 2>&1; docker rm -f ${GXMAX_RANK1_NAME} >/dev/null 2>&1; true" >/dev/null \
  || warn "could not reach node2 to stop rank1 in ${N2_TRIES} attempts -- the node-2 deadman (rank1-deadman.sh) is the remaining defence; re-checking below"

# ------------------------------------------------ 3. confirm both are gone --
# `docker rm -f` returns as soon as the daemon accepts the request; the
# container is reaped by containerd a moment later. Sampling State.Running
# once, immediately, reports a still-running container that is in fact
# already dying -- that produced a spurious FAIL the first time this unwind
# was exercised (2026-09-16). Poll for the real terminal state instead, and
# only fail if it never arrives.
CONFIRM_TRIES="${GXMAX_UNWIND_CONFIRM_TRIES:-15}"
r0_state=""
for (( i=1; i<=CONFIRM_TRIES; i++ )); do
  # `docker inspect` on a missing container writes a BLANK LINE to stdout
  # before failing, so `... || echo absent` yields "\nabsent" and a bare
  # case-match on "absent" misses. Normalise whitespace and treat empty as
  # absent -- this is what made the first two unwind runs report a
  # still-running rank0 that did not exist at all (2026-09-16).
  r0_state="$(docker inspect -f '{{.State.Running}}' "${GXMAX_RANK0_NAME}" 2>/dev/null | tr -d '[:space:]')"
  [ -z "${r0_state}" ] && r0_state=absent
  case "${r0_state}" in absent|false) break ;; esac
  sleep 2
done
case "${r0_state}" in
  absent|false) ok "rank0 confirmed not running (${r0_state})" ;;
  *)            fail "rank0 is STILL RUNNING on node1 after stop+kill+rm and ${CONFIRM_TRIES} confirmations" ;;
esac

r1_state=""
for (( i=1; i<=CONFIRM_TRIES; i++ )); do
  if r1_state="$(n2_retry "docker inspect -f '{{.State.Running}}' ${GXMAX_RANK1_NAME} 2>/dev/null; true")"; then
    r1_state="$(printf '%s' "${r1_state}" | tr -d '[:space:]')"
    [ -z "${r1_state}" ] && r1_state=absent
  else
    r1_state=unreachable
  fi
  case "${r1_state}" in absent|false) break ;; esac
  sleep 2
done
case "${r1_state}" in
  absent|false) ok "rank1 confirmed not running (${r1_state})" ;;
  unreachable)  fail "rank1 state UNKNOWN -- node2 unreachable. Deadman should fire within its grace window; see RECOVERY.md B-020 escalation" ;;
  *)            fail "rank1 is STILL RUNNING on node2 after stop+kill+rm and ${CONFIRM_TRIES} confirmations" ;;
esac

# ------------------------------------- 4. clean stale rank containers/procs --
docker ps -a --filter "name=^/${GXMAX_RANK0_NAME}$" --format '{{.Names}}' | while read -r c; do
  [ -n "${c}" ] && { log "unwind: removing stale container ${c} on node1"; docker rm -f "${c}" >/dev/null 2>&1 || true; }
done
n2_retry "docker ps -a --filter 'name=^/${GXMAX_RANK1_NAME}\$' --format '{{.Names}}' | xargs -r docker rm -f >/dev/null 2>&1; if [ -f \$HOME/.gx-guard/rank1-deadman.pid ]; then kill \$(cat \$HOME/.gx-guard/rank1-deadman.pid) >/dev/null 2>&1; rm -f \$HOME/.gx-guard/rank1-deadman.pid; fi; true" >/dev/null 2>&1 || true

# --------------------------------------------- 5+6. reconcile both ledgers --
gx_guard_release node1 "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || warn "node1 ledger release returned non-zero"
gx_guard_release node2 "${GXMAX_RANK1_NAME}" >/dev/null 2>&1 || warn "node2 ledger release returned non-zero"
led1="$(gx_guard_status node1 2>/dev/null || echo '?')"
led2="$(gx_guard_status node2 2>/dev/null || echo '?')"
case "${led1}" in *gx-max*) fail "node1 ledger still lists a gx-max entry: ${led1}" ;; *) ok "node1 ledger clean: ${led1}" ;; esac
case "${led2}" in *gx-max*) fail "node2 ledger still lists a gx-max entry: ${led2}" ;; *) ok "node2 ledger clean: ${led2}" ;; esac

# -------------------------------------------------------- 7. release locks --
# flock(2) locks are released when the holding fd closes, so a lock can only
# be "stuck" if a process is still alive holding it. Prove that, do not assume.
for node in node1 node2; do
  lf="${GX_GUARD_STATE_DIR}/${node}.lock"
  [ -f "${lf}" ] || { ok "${node} lock file absent (nothing held)"; continue; }
  if flock -x -w 5 "${lf}" true 2>/dev/null; then
    ok "${node} lock is free"
  else
    holders="$(command -v fuser >/dev/null 2>&1 && fuser "${lf}" 2>&1 || echo '?')"
    fail "${node} lock still held after unwind (holders: ${holders})"
  fi
done
# node2's own remote convention lock (see gx-max-start.sh)
n2_retry "flock -x -w 5 \$HOME/.gx-guard/node2.lock true 2>/dev/null && echo free || echo held" >/dev/null 2>&1 \
  && ok "node2 remote convention lock checked" || warn "could not check node2's remote lock"

# ---------------------------------- 8. restore node2 (and node1) services --
if [ "${RESTORE}" -eq 1 ]; then
  log "unwind: restoring normal services"
  if [ -x "${here}/restore-normal.sh" ]; then
    "${here}/restore-normal.sh" >/dev/null 2>&1 || warn "restore-normal.sh reported a problem"
  fi
  n2_ls="$(n2_retry "docker inspect -f '{{.State.Running}}' gx-llama-swap-node02 2>/dev/null; true" || echo unreachable)"
  n2_ls="$(printf '%s' "${n2_ls}" | tr -d '[:space:]')"
  [ -z "${n2_ls}" ] && n2_ls=absent
  [ "${n2_ls}" = "true" ] && ok "node2 llama-swap is running" || warn "node2 llama-swap is '${n2_ls}' after restore"
fi

# ------------------------------------------- 9. verify memory came back --
log "unwind: waiting 15s for the kernel to reclaim unified-memory allocations"
sleep 15
avail1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
avail2=$(n2_retry "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" || echo "")
avail2="$(printf '%s' "${avail2}" | tr -d '[:space:]')"
FLOOR="${GX_GUARD_RESERVE_GIB:-30}"
[ "${avail1:-0}" -ge "${FLOOR}" ] && ok "node1 MemAvailable ${avail1}GiB >= ${FLOOR}GiB floor" || fail "node1 MemAvailable ${avail1}GiB is BELOW the ${FLOOR}GiB floor after unwind"
if [ -n "${avail2}" ]; then
  [ "${avail2}" -ge "${FLOOR}" ] && ok "node2 MemAvailable ${avail2}GiB >= ${FLOOR}GiB floor" || fail "node2 MemAvailable ${avail2}GiB is BELOW the ${FLOOR}GiB floor after unwind"
else
  fail "could not read node2 MemAvailable after unwind"
fi

# ----------------------------------------- 10. verify swap stopped growing --
swap_used_n1_a=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)
sleep 10
swap_used_n1_b=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)
if [ "${swap_used_n1_b}" -le "${swap_used_n1_a}" ]; then
  ok "node1 swap is not growing (${swap_used_n1_before}MiB at entry -> ${swap_used_n1_a} -> ${swap_used_n1_b}MiB)"
else
  fail "node1 swap is STILL GROWING (${swap_used_n1_a} -> ${swap_used_n1_b}MiB) -- something is still allocating"
fi
s2a=$(n2_retry "awk '/SwapTotal/{t=\$2} /SwapFree/{f=\$2} END{print int((t-f)/1024)}' /proc/meminfo" || echo "")
sleep 10
s2b=$(n2_retry "awk '/SwapTotal/{t=\$2} /SwapFree/{f=\$2} END{print int((t-f)/1024)}' /proc/meminfo" || echo "")
s2a="$(printf '%s' "${s2a}" | tr -d '[:space:]')"; s2b="$(printf '%s' "${s2b}" | tr -d '[:space:]')"
if [ -n "${s2a}" ] && [ -n "${s2b}" ]; then
  [ "${s2b}" -le "${s2a}" ] && ok "node2 swap is not growing (${swap_used_n2_before:-?}MiB at entry -> ${s2a} -> ${s2b}MiB)" \
                            || fail "node2 swap is STILL GROWING (${s2a} -> ${s2b}MiB)"
else
  warn "could not sample node2 swap twice"
fi

# ------------------------------- 11. verify SSH / Tailscale still healthy --
if n2_retry true >/dev/null 2>&1; then ok "node2 SSH over Tailscale is healthy"; else fail "node2 SSH is NOT healthy after unwind"; fi
if command -v tailscale >/dev/null 2>&1; then
  ts="$(tailscale status --peers=false 2>&1 | head -1)"
  case "${ts}" in *Logged\ out*|*stopped*) fail "tailscale is not up: ${ts}" ;; *) ok "tailscale up: ${ts}" ;; esac
else
  warn "tailscale CLI not available on node1; skipped"
fi
# Same probe gx-max-start.sh's preflight uses, and for the same reason: a
# fast "connection refused" proves the far kernel answered, while a timeout
# means genuinely unreachable. The first version of this check piped the
# probe into grep, which swallowed the exit status and reported both healthy
# rails as unreachable (2026-09-16).
rail_reachable() {
  local peer="$1" out rc
  out=$(timeout 2 bash -c "exec 3<>/dev/tcp/${peer}/1" 2>&1); rc=$?
  [ "${rc}" -eq 0 ] && return 0
  printf '%s' "${out}" | grep -qi 'connection refused'
}
for peer in 192.168.100.11 192.168.101.11; do
  if rail_reachable "${peer}"; then ok "ConnectX rail ${peer} reachable"
  else warn "ConnectX rail ${peer} did not answer"; fi
done

if [ "${FAILED}" -eq 0 ]; then
  log "================= gx-max UNWIND COMPLETE — cluster verified clean ================="
  exit 0
fi
log "================= gx-max UNWIND INCOMPLETE — see FAIL lines above ================="
exit 1
