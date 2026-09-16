#!/usr/bin/env bash
# ============================================================================
# rank1-deadman.sh — node-2-side self-termination watchdog for gx-max-rank1.
#
# WHY THIS EXISTS (coordination/BLOCKERS.md B-020)
# ------------------------------------------------
# On 2026-09-15 rank0 was OOM-killed on node 1 during weight loading. The
# orchestrator's unwind (D-017) then tried to `ssh` to node 2 to stop rank1 --
# and the ssh itself timed out, because rank1 had *already* starved node 2's
# userspace. The orphan then held ~90 GiB of node 2 for 80 minutes until the
# kernel OOM-killed it. Every remote cleanup path shares that flaw: it needs a
# healthy node 2 exactly when node 2 is least healthy.
#
# The fix is to not depend on the remote path at all. This script runs ON
# node 2, is started immediately after rank1, and is already resident (a few
# hundred KiB of bash, no allocation in its steady-state loop) by the time any
# memory pressure appears. It watches rank0's liveness over the ConnectX
# fabric and force-removes rank1 itself when rank0 is gone.
#
# LIVENESS SIGNAL: a TCP connect to rank0's torch-distributed bootstrap store
# (${DIST_ADDR}). rank0 -- and only rank0 -- binds that socket, for the entire
# life of the engine. It is on the RoCE fabric, not Tailscale (L-3), and a
# bare TCP connect needs no capability (the same reason gx-max-start.sh's
# preflight stopped using `ping` -- see its fabric_rail_reachable comment).
#
# Exits (and stops watching) when rank1 is gone, which makes a normal
# gx-max-stop.sh teardown clean this up for free.
#
# LOCAL SAFETY (D-025): the same gx-max-safety.sh rules node 1 applies to
# itself -- kernel OOM kill, NV_ERR_NO_MEMORY, sustained memory+swap
# exhaustion, sustained swap thrashing, sustained fork/exec starvation, and
# (once serving) a sustained steady-state floor. A single low MemAvailable
# sample is NOT a trip: loading a rank legitimately spills into
# /swapfile-sglang and the verified 2026-09-14 launch touched ~1 GiB.
#
# PHASE: the engine is "steady" once rank0's /health answers. rank0 serves
# HTTP on node 1 only, so the probe goes to node 1 over the fabric
# (<health_url>, default http://192.168.100.10:30000/health). The previous
# version probed 127.0.0.1 on node 2, which never answers, so node 2 stayed
# in the load phase forever.
#
# Usage: rank1-deadman.sh <dist_addr host:port> <container> <startup_grace_s> \
#            <death_grace_s> [poll_s] [health_url] [sample_log]
# Tunables are read from GXMAX_* environment variables (see gx-max.conf).
# ============================================================================
set -uo pipefail

DIST_ADDR="${1:?dist addr host:port required}"
CONTAINER="${2:?rank1 container name required}"
STARTUP_GRACE="${3:-600}"   # rank0 may legitimately not exist yet: rank1 starts first
DEATH_GRACE="${4:-120}"     # consecutive seconds of rank0-unreachable before we fire
POLL="${5:-10}"
HEALTH_URL="${6:-http://${DIST_ADDR%%:*}:30000/health}"
SAMPLE_LOG="${7:-${HOME}/gx-max-node2-mem.tsv}"

DIST_HOST="${DIST_ADDR%%:*}"
DIST_PORT="${DIST_ADDR##*:}"
# shellcheck source=./gx-max-safety.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gx-max-safety.sh"
LOG="${HOME}/gx-max-rank1-deadman.log"

# PID file, not `pkill -f`. `pkill -f rank1-deadman.sh` matches the *ssh
# remote command line* that starts this script -- i.e. the remote shell
# kills itself before the deadman is ever armed. Found the first time the
# arming path was exercised (2026-09-16). A pid file has no such ambiguity.
PIDFILE="${HOME}/.gx-guard/rank1-deadman.pid"
mkdir -p "$(dirname "${PIDFILE}")"
if [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}" 2>/dev/null)" 2>/dev/null; then
  kill "$(cat "${PIDFILE}")" 2>/dev/null || true
  sleep 1
fi
echo $$ > "${PIDFILE}"
trap 'rm -f "${PIDFILE}"' EXIT

log() { printf '[%s] deadman: %s\n' "$(date -Is)" "$*" >>"${LOG}"; }

rank0_alive() { timeout 3 bash -c "exec 3<>/dev/tcp/${DIST_HOST}/${DIST_PORT}" 2>/dev/null; }
rank1_present() { [ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" = "true" ]; }

engine_ready() { curl -fsS -m 3 "${HEALTH_URL}" >/dev/null 2>&1; }
steady=0

restore_node2() {
  if [ -f "${HOME}/gx-gateway/docker-compose.node02.yml" ]; then
    ( cd "${HOME}/gx-gateway" && docker compose -f docker-compose.node02.yml up -d ) >>"${LOG}" 2>&1 \
      && log "node-2 llama-swap restored" || log "WARN: could not restore node-2 llama-swap"
  fi
}

gxs_arm
log "started: watching ${DIST_ADDR} for rank0; container=${CONTAINER} startup_grace=${STARTUP_GRACE}s death_grace=${DEATH_GRACE}s poll=${POLL}s health=${HEALTH_URL} crit_avail=${GXMAX_CRIT_AVAIL_MIB}MiB crit_swapfree=${GXMAX_CRIT_SWAPFREE_MIB}MiB steady_floor=${GXMAX_STEADY_FLOOR_GIB}GiB"
gxs_header > "${SAMPLE_LOG}"

started_at=$(date +%s)
seen_rank0=0
lost_since=0

while :; do
  sleep "${POLL}"

  if ! rank1_present; then
    log "rank1 is no longer running; nothing to guard. Exiting."
    exit 0
  fi

  # Phase latch: once rank0 answers /health the load transient is over.
  if [ "${steady}" -eq 0 ] && engine_ready; then
    steady=1
    log "engine answered ${HEALTH_URL}: switching to steady-phase rules"
  fi

  if [ "${steady}" -eq 1 ]; then gxs_tick steady; else gxs_tick load; fi
  printf '%s\n' "${GXS_SAMPLE}" >> "${SAMPLE_LOG}"

  # Local trip: needs no network, fires while the host can still run docker.
  if [ "${GXS_VERDICT}" = abort ]; then
    log "FIRING (local safety): ${GXS_REASON}"
    docker rm -f "${CONTAINER}" >>"${LOG}" 2>&1 || log "WARN: docker rm -f ${CONTAINER} failed"
    sleep 10
    _gxs_read
    log "rank1 removed on local safety trip. MemAvailable now ${GXS_AVAIL_MIB}MiB; min seen ${GXS_MIN_AVAIL_MIB}MiB, max swap used ${GXS_MAX_SWAPUSED_MIB}MiB"
    restore_node2
    exit 0
  fi

  if rank0_alive; then
    if [ "${seen_rank0}" -eq 0 ]; then
      log "rank0 bootstrap store is reachable; deadman is now armed"
      seen_rank0=1
    fi
    lost_since=0
    continue
  fi

  now=$(date +%s)

  # Before rank0 has EVER answered we are still inside the startup window:
  # rank1 is deliberately started first and waits for rank0 to appear.
  if [ "${seen_rank0}" -eq 0 ]; then
    if [ $(( now - started_at )) -lt "${STARTUP_GRACE}" ]; then
      continue
    fi
    log "FIRING: rank0 never appeared at ${DIST_ADDR} within ${STARTUP_GRACE}s of rank1 starting"
  else
    [ "${lost_since}" -eq 0 ] && { lost_since=${now}; log "rank0 unreachable at ${DIST_ADDR}; grace ${DEATH_GRACE}s"; continue; }
    if [ $(( now - lost_since )) -lt "${DEATH_GRACE}" ]; then
      continue
    fi
    log "FIRING: rank0 unreachable for $(( now - lost_since ))s (>= ${DEATH_GRACE}s grace)"
  fi

  # Fire. Force-remove rank1 so node 2's memory comes back without needing
  # anyone to reach this host from outside.
  avail_before=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  docker rm -f "${CONTAINER}" >>"${LOG}" 2>&1 || log "WARN: docker rm -f ${CONTAINER} failed"
  sleep 10
  avail_after=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  log "rank1 removed. MemAvailable ${avail_before}GiB -> ${avail_after}GiB"

  # Hand node 2 back to normal service if its control plane was drained.
  restore_node2
  exit 0
done
