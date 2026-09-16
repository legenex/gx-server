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
# TWO MEMORY FLOORS, BECAUSE THERE ARE TWO PHASES (measured 2026-09-16)
# -----------------------------------------------------------------------
# Loading one TP=2 rank of DeepSeek-V4-Flash drives a 121.63 GiB node down
# to 1-5 GiB MemAvailable for ~60-120 s, and `--mem-fraction-static` does
# NOT change that: measured at 0.50 and at 0.70 the trough is the same,
# because the trough is the model weights landing in NVIDIA-driver-held
# unified memory (invisible to every /proc/meminfo LRU counter -- B-021),
# not the KV/static pool. Enforcing the steady-state reserve during that
# window just aborts every launch, which is what the first two guarded runs
# did. So:
#
#   LOAD phase   (engine not yet answering /health): only a last-resort
#                floor, low enough to ride the known transient but high
#                enough that WE tear down rather than the kernel OOM killer.
#   STEADY phase (engine has answered /health once): the real reserve floor.
#
# SECOND TRIP CONDITION: node-2 memory floor. B-012 and B-020 are the same
# shape -- node 2's userspace starves while its kernel stays alive, so every
# remote probe stops working precisely when intervention is needed. A local
# watcher can act while the host is still healthy enough to act. If node 2's
# real MemAvailable falls below <abort_floor_gib>, rank1 removes itself. A
# run that trips this has failed its memory budget; that is a correct abort,
# not a false positive.
#
# Usage: rank1-deadman.sh <dist_addr host:port> <container> <startup_grace_s> \
#            <death_grace_s> [poll_s] [abort_floor_gib] [sample_log]
# ============================================================================
set -uo pipefail

DIST_ADDR="${1:?dist addr host:port required}"
CONTAINER="${2:?rank1 container name required}"
STARTUP_GRACE="${3:-600}"   # rank0 may legitimately not exist yet: rank1 starts first
DEATH_GRACE="${4:-120}"     # consecutive seconds of rank0-unreachable before we fire
POLL="${5:-10}"
ABORT_FLOOR_GIB="${6:-0}"        # STEADY-STATE floor; 0 disables the memory trip
SAMPLE_LOG="${7:-${HOME}/gx-max-node2-mem.tsv}"
LOAD_FLOOR_GIB="${8:-2}"         # LOAD-PHASE floor (see phase note below)
HEALTH_URL="${9:-http://127.0.0.1:30000/health}"

DIST_HOST="${DIST_ADDR%%:*}"
DIST_PORT="${DIST_ADDR##*:}"
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

mem_avail_gib() { awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo; }
swap_used_mib() { awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo; }

engine_ready() { curl -fsS -m 3 "${HEALTH_URL}" >/dev/null 2>&1; }
steady=0

log "started: watching ${DIST_ADDR} for rank0; container=${CONTAINER} startup_grace=${STARTUP_GRACE}s death_grace=${DEATH_GRACE}s poll=${POLL}s load_floor=${LOAD_FLOOR_GIB}GiB steady_floor=${ABORT_FLOOR_GIB}GiB"
: > "${SAMPLE_LOG}"
printf 'epoch\tmem_available_gib\tswap_used_mib\n' >> "${SAMPLE_LOG}"

started_at=$(date +%s)
seen_rank0=0
lost_since=0

while :; do
  sleep "${POLL}"

  printf '%s\t%s\t%s\n' "$(date +%s)" "$(mem_avail_gib)" "$(swap_used_mib)" >> "${SAMPLE_LOG}"

  if ! rank1_present; then
    log "rank1 is no longer running; nothing to guard. Exiting."
    exit 0
  fi

  # Phase latch: once the engine has answered /health, the load transient is
  # over and the real reserve floor applies from then on.
  if [ "${steady}" -eq 0 ] && engine_ready; then
    steady=1
    log "engine answered ${HEALTH_URL}: switching from load floor ${LOAD_FLOOR_GIB}GiB to steady floor ${ABORT_FLOOR_GIB}GiB"
  fi

  # Memory trip: local, needs no network, fires while the host is still
  # responsive enough to run `docker rm -f`.
  if [ "${steady}" -eq 1 ]; then floor="${ABORT_FLOOR_GIB}"; else floor="${LOAD_FLOOR_GIB}"; fi
  if [ "${floor}" -gt 0 ]; then
    avail=$(mem_avail_gib)
    if [ "${avail}" -lt "${floor}" ]; then
      log "FIRING (memory): node2 MemAvailable ${avail}GiB < $([ "${steady}" -eq 1 ] && echo steady || echo load) floor ${floor}GiB"
      docker rm -f "${CONTAINER}" >>"${LOG}" 2>&1 || log "WARN: docker rm -f ${CONTAINER} failed"
      sleep 10
      log "rank1 removed on memory floor. MemAvailable now $(mem_avail_gib)GiB"
      if [ -f "${HOME}/gx-gateway/docker-compose.node02.yml" ]; then
        ( cd "${HOME}/gx-gateway" && docker compose -f docker-compose.node02.yml up -d ) >>"${LOG}" 2>&1 \
          && log "node-2 llama-swap restored" || log "WARN: could not restore node-2 llama-swap"
      fi
      exit 0
    fi
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
  if [ -f "${HOME}/gx-gateway/docker-compose.node02.yml" ]; then
    ( cd "${HOME}/gx-gateway" && docker compose -f docker-compose.node02.yml up -d ) >>"${LOG}" 2>&1 \
      && log "node-2 llama-swap restored" || log "WARN: could not restore node-2 llama-swap"
  fi
  exit 0
done
