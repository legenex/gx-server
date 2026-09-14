#!/usr/bin/env bash
# ============================================================================
# gx-max-stop.sh — release BOTH nodes and return the cluster to normal state.
#
# Default behaviour is a GRACEFUL DRAIN: we wait for in-flight inference to
# finish before tearing the ranks down. Active jobs are not interrupted unless
# --force is given.
#
#   --force        stop immediately, do not wait for in-flight requests
#   --grace <sec>  how long to wait for the queue to drain (default 300)
#   --no-restore   do not restart normal single-node workloads afterwards
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"

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
      # Metrics are not exposed on this server instance. We cannot observe the
      # queue, so fall back to a fixed quiet period rather than pretending the
      # server is idle.
      log "  /metrics unavailable; falling back to a ${GRACE}s fixed quiet period"
      sleep "${GRACE}"
      break
    fi
    if [ "${running}" -eq 0 ]; then log "queue empty, proceeding"; break; fi
    log "  ${running} request(s) still in flight..."
    sleep 5
  done
fi

# ------------------------------------------------------------------ teardown --
# rank0 first (it owns the HTTP server and the bootstrap store), then rank1.
log "stopping rank0 on node1"
docker stop -t 30 "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true
docker rm -f "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true

log "stopping rank1 on node2"
n2 "docker stop -t 30 ${GXMAX_RANK1_NAME} >/dev/null 2>&1 || true; docker rm -f ${GXMAX_RANK1_NAME} >/dev/null 2>&1 || true"

# Give the kernel a moment to actually reclaim the unified-memory allocations.
sleep 5
log "MemAvailable after release: node1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)GiB node2=$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo")GiB"

# ------------------------------------------------------------------ restore --
if [ "${RESTORE}" -eq 1 ]; then
  log "restoring normal single-node workloads"
  restore="${here}/../lifecycle/restore-normal.sh"
  if [ -x "${restore}" ]; then
    "${restore}" || log "WARN: restore-normal.sh reported a problem"
  else
    log "  (no restore-normal.sh present yet; skipping)"
  fi
fi

log "gx-max released; both nodes are back to normal operating state"
