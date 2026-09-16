#!/usr/bin/env bash
# ============================================================================
# rank0-watch.sh — node-1-side steady-state watchdog for a SERVING gx-max.
#
# The mirror image of rank1-deadman.sh. gx-max-start.sh exits once the engine
# is READY, and from then on nothing on node 1 was watching the other half of
# the job: if rank1 died, node 2's deadman simply exited ("nothing to
# guard") and rank0 stayed resident on node 1 -- ~105 GiB held by an engine
# that can no longer serve. This closes that gap. Started (detached) by
# gx-max-start.sh on READY; gx-max-stop.sh and gx-max-unwind.sh stop it
# before they tear anything down.
#
# Fires gx-max-unwind.sh (both nodes, confirmed teardown, service restore)
# when ANY of these holds:
#   * rank1 is confirmed not running on node 2 on two consecutive checks
#   * node 2 has been unreachable over SSH for >= GXMAX_N2_UNREACHABLE_ABORT_S
#     (rank0 cannot serve without it; node 2's deadman handles its own side)
#   * rank0's /health has failed continuously for >= GXMAX_HEALTH_FAIL_ABORT_S
#     while the container is still running (a wedged engine)
#   * node 1's own gx-max-safety.sh steady-phase rules trip
#
# Usage: rank0-watch.sh  (reads gx-max.conf; logs to $GXMAX_LOG_DIR/gx-max-rank0-watch.log)
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${here}/lib.sh"
# shellcheck source=./gx-max-safety.sh
source "${here}/gx-max-safety.sh"

POLL="${GXMAX_WATCH_POLL:-15}"
HEALTH_FAIL_ABORT_S="${GXMAX_HEALTH_FAIL_ABORT_S:-180}"
LOG="${GXMAX_LOG_DIR}/gx-max-rank0-watch.log"
PIDFILE="${GX_STATE_ROOT:-/srv/projects/gx-cluster/state}/gx-max-rank0-watch.pid"
mkdir -p "$(dirname "${PIDFILE}")"

wlog() { printf '[%s] rank0-watch: %s\n' "$(date -Is)" "$*" >> "${LOG}"; }

if [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}" 2>/dev/null)" 2>/dev/null; then
  wlog "another watcher (pid $(cat "${PIDFILE}")) is already running; exiting"
  exit 0
fi
echo $$ > "${PIDFILE}"
trap 'rm -f "${PIDFILE}"' EXIT

fire() {
  wlog "FIRING: $1 -- running gx-max-unwind.sh"
  bash "${here}/gx-max-unwind.sh" --reason "rank0-watch: $1" >> "${LOG}" 2>&1 \
    && wlog "unwind reported a verified-clean cluster" \
    || wlog "UNWIND REPORTED PROBLEMS -- see the FAIL lines above"
  exit 0
}

rank1_state() { # prints true|false|absent|unreachable
  local s
  if s="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${GXMAX_NODE2_SSH}" \
          "docker inspect -f '{{.State.Running}}' ${GXMAX_RANK1_NAME} 2>/dev/null; true" 2>/dev/null)"; then
    s="$(printf '%s' "${s}" | tr -d '[:space:]')"
    printf '%s' "${s:-absent}"
  else
    printf 'unreachable'
  fi
}

gxs_arm
wlog "started: poll ${POLL}s, node2-unreachable abort ${GXMAX_N2_UNREACHABLE_ABORT_S}s, health-fail abort ${HEALTH_FAIL_ABORT_S}s"
r1_gone=0; n2_bad_since=0; health_bad_since=0

while :; do
  sleep "${POLL}"
  now=$(date +%s)

  # A deliberate release (gx-max-stop.sh / gx-max-unwind.sh) kills this
  # watcher BEFORE touching rank0, so a rank0 that vanishes while we are
  # still alive vanished unexpectedly (OOM kill, crash, manual docker rm).
  # rank1 may still be resident and node 1's normal services are drained:
  # unwind both nodes.
  if ! gxmax_rank0_running; then
    fire "rank0 disappeared from node1"
  fi

  gxs_tick steady
  [ "${GXS_VERDICT}" = abort ] && fire "node1 safety: ${GXS_REASON}"

  case "$(rank1_state)" in
    true) r1_gone=0; n2_bad_since=0 ;;
    false|absent)
      n2_bad_since=0
      r1_gone=$(( r1_gone + 1 ))
      wlog "rank1 not running on node2 (check ${r1_gone}/2)"
      [ "${r1_gone}" -ge 2 ] && fire "rank1 is gone from node2" ;;
    *)
      [ "${n2_bad_since}" -eq 0 ] && { n2_bad_since="${now}"; wlog "node2 unreachable over SSH"; }
      [ $(( now - n2_bad_since )) -ge "${GXMAX_N2_UNREACHABLE_ABORT_S}" ] && fire "node2 unreachable for $(( now - n2_bad_since ))s" ;;
  esac

  if gxmax_healthy; then
    health_bad_since=0
  else
    [ "${health_bad_since}" -eq 0 ] && { health_bad_since="${now}"; wlog "rank0 /health failing"; }
    [ $(( now - health_bad_since )) -ge "${HEALTH_FAIL_ABORT_S}" ] && fire "rank0 /health failing for $(( now - health_bad_since ))s"
  fi
done
