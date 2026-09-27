#!/usr/bin/env bash
# ============================================================================
# gx-max-stop.sh (V4.1) — release BOTH nodes and hand the cluster back.
#
# Thin, cluster-aware teardown around the two Mia containers. The drain /
# ledger / hold / restore sequence is the cluster contract and stays here;
# the containers themselves (dsv41-exl3-head on node 1, dsv41-exl3-worker on
# node 2) are stopped in rank order: head first (it owns the HTTP API and
# the TP rendezvous), then the worker.
#
# Marker lines (draining / stopping / memory recovery / released) are parsed
# by gx_orchestrator.lifecycle for its phase tracking -- keep them.
#
#   --force        stop immediately, do not wait for in-flight requests
#   --grace <sec>  how long to wait for the queue to drain (default 300)
#   --no-restore   do not restart normal single-node workloads afterwards
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"
# shellcheck source=./node2-holds.sh
source "${here}/node2-holds.sh"

FORCE=0; GRACE=300; RESTORE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --grace) GRACE="$2"; shift ;;
    --no-restore) RESTORE=0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

# ------------------------------------------------------------------- drain --
if [ "${FORCE}" -eq 0 ] && gxmax_healthy; then
  log "draining: waiting up to ${GRACE}s for in-flight requests to finish"
  deadline=$(( $(date +%s) + GRACE ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    running=$(gxmax_inflight)
    if [ "${running}" = "unknown" ]; then
      # Metrics are not reachable. We cannot observe the queue, so fall back
      # to a fixed quiet period rather than pretending the server is idle.
      log "  /metrics unavailable; falling back to a ${GRACE}s fixed quiet period"
      sleep "${GRACE}"
      break
    fi
    if [ "${running}" -eq 0 ]; then log "queue empty, proceeding"; break; fi
    log "  ${running} request(s) still in flight..."
    sleep 5
  done
fi

# ---------------------------------------------------------------- teardown --
# A deliberate release is not a failure: stop node 1's steady-state watcher
# first so it cannot mistake this teardown for a dead rank and start an unwind.
wpid="${GX_STATE_ROOT:-/srv/projects/gx-cluster/state}/gx-max-rank0-watch.pid"
[ -f "${wpid}" ] && kill "$(cat "${wpid}")" >/dev/null 2>&1 || true
rm -f "${wpid}"

log "stopping the Mia containers (${GXMAX_RANK0_NAME} on node1, ${GXMAX_RANK1_NAME} on node2)"
docker stop -t 30 "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true
docker rm -f "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true
n2 "docker stop -t 30 ${GXMAX_RANK1_NAME} >/dev/null 2>&1 || true; docker rm -f ${GXMAX_RANK1_NAME} >/dev/null 2>&1 || true"

# Release both residency-ledger entries so the admission guard's ledger math
# (resource-guard.sh / gx_orchestrator.resource_guard) reflects reality
# immediately rather than waiting for the next reconcile.
gx_guard_release node1 gx-max-rank0 || true
gx_guard_release node2 gx-max-rank1 || true

# The drain window is over: lift the gx-max hold on node 2 (D-036).
gx_n2_hold_clear gxmax || log "WARN: could not clear the gx-max hold on node2"

# Give the kernel a moment to actually reclaim the unified-memory allocations.
sleep 5
log "MemAvailable after release: node1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)GiB node2=$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo")GiB"

# ---------------------------------------------------------------- restore --
if [ "${RESTORE}" -eq 1 ]; then
  log "restoring normal single-node workloads"
  restore="${here}/restore-normal.sh"
  if [ -x "${restore}" ]; then
    "${restore}" || log "WARN: restore-normal.sh reported a problem"
  else
    log "  (no restore-normal.sh present; skipping)"
  fi
fi

log "gx-max released; both nodes are back to normal operating state"
