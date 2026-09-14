#!/usr/bin/env bash
# ============================================================================
# gx-max-start.sh — acquire BOTH nodes and bring up DeepSeek V4 Flash (SGLang TP=2)
#
# Sequence (as specified in the locked architecture):
#   preflight -> drain conflicting GPU work -> start rank1 -> start rank0
#   -> wait for health -> report
#
# Rank order matters: rank1 is started FIRST and retries its connection to the
# rank0 bootstrap store at ${GXMAX_DIST_ADDR}. This mirrors the verified-working
# run (rank1 came up 12s before rank0).
#
# Exit codes: 0 ready | 1 preflight failure | 2 startup timeout | 3 rank died
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"

FORCE_DRAIN="${GXMAX_FORCE_DRAIN:-0}"

# Containers that must not hold GPU/unified memory while gx-max runs.
CONFLICTS_N1=(gx-mini gx-fast vllm-qwen38-uncensored vllm llama-swap-node01)
CONFLICTS_N2=(gx-reason comfyui llama-swap-node02)

# ---------------------------------------------------------------- preflight --
log "=== gx-max preflight ==="

if gxmax_healthy; then
  log "gx-max is ALREADY healthy on port ${GXMAX_PORT}. Nothing to do."
  exit 0
fi

[ -d "${GXMAX_MODEL_DIR}" ] || die "model dir missing on node1: ${GXMAX_MODEL_DIR}"
n2 "test -d '${GXMAX_MODEL_DIR}'" || die "model dir missing on node2: ${GXMAX_MODEL_DIR}"
docker image inspect "${GXMAX_IMAGE}" >/dev/null 2>&1 || die "image missing on node1: ${GXMAX_IMAGE}"
n2 "docker image inspect '${GXMAX_IMAGE}' >/dev/null 2>&1" || die "image missing on node2: ${GXMAX_IMAGE}"

# Fabric must be alive on both rails before we try a two-node job.
for peer in 192.168.100.11 192.168.101.11; do
  ping -c 2 -W 2 "${peer}" >/dev/null 2>&1 || die "ConnectX rail unreachable: ${peer}"
done
log "both ConnectX rails reachable"

mkdir -p "${GXMAX_CACHE_DIR}" "${GXMAX_LOG_DIR}"
n2 "mkdir -p '${GXMAX_CACHE_DIR}'"

# ------------------------------------------------------------------- drain --
# Do not interrupt active inference by default: we stop conflicting containers
# gracefully (SIGTERM + grace period) so in-flight requests can finish.
log "=== draining conflicting GPU work ==="
drain_node1() {
  for c in "${CONFLICTS_N1[@]}"; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo false)" = "true" ]; then
      log "  node1: stopping ${c} (graceful, 60s)"
      docker stop -t 60 "$c" >/dev/null || log "  node1: WARN failed to stop ${c}"
    fi
  done
}
drain_node2() {
  for c in "${CONFLICTS_N2[@]}"; do
    n2 "if [ \"\$(docker inspect -f '{{.State.Running}}' $c 2>/dev/null || echo false)\" = true ]; then echo '  node2: stopping $c'; docker stop -t 60 $c >/dev/null || echo '  node2: WARN failed to stop $c'; fi"
  done
}
drain_node1
drain_node2

# Remove any stale rank containers left by a previous run.
docker rm -f "${GXMAX_RANK0_NAME}" >/dev/null 2>&1 || true
n2 "docker rm -f ${GXMAX_RANK1_NAME} >/dev/null 2>&1 || true"

# Memory sanity: SGLang needs roughly the whole node. Warn loudly if something
# large is still resident, but do not silently kill unknown workloads.
avail_n1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
avail_n2=$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo")
log "MemAvailable: node1=${avail_n1}GiB node2=${avail_n2}GiB"
if [ "${avail_n1}" -lt 90 ] || [ "${avail_n2}" -lt 90 ]; then
  if [ "${FORCE_DRAIN}" = "1" ]; then
    log "WARN: low free memory but GXMAX_FORCE_DRAIN=1, continuing"
  else
    die "not enough free memory (need ~90GiB/node). Something is still holding memory. Inspect with 'docker ps' on both nodes, or re-run with GXMAX_FORCE_DRAIN=1 to override."
  fi
fi

# ------------------------------------------------------------------- start --
mapfile -t DFLAGS < <(gxmax_docker_flags)
mapfile -t EFLAGS < <(gxmax_env_flags)

log "=== starting rank1 on node2 ==="
# Build the remote command as a single properly quoted string.
r1_cmd=$(printf '%q ' docker run -d --name "${GXMAX_RANK1_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 1))
n2 "${r1_cmd}" >/dev/null || die "failed to start rank1"
n2 "docker logs -f ${GXMAX_RANK1_NAME} > \$HOME/gx-max-rank1.log 2>&1 &" >/dev/null 2>&1 || true
log "rank1 started"

sleep 5

log "=== starting rank0 on node1 ==="
docker run -d --name "${GXMAX_RANK0_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 0) >/dev/null \
  || die "failed to start rank0"
( docker logs -f "${GXMAX_RANK0_NAME}" > "${GXMAX_LOG_DIR}/gx-max-rank0.log" 2>&1 & ) || true
log "rank0 started"

# ------------------------------------------------------------- wait healthy --
log "=== waiting for gx-max to become healthy (timeout ${GXMAX_READY_TIMEOUT}s) ==="
log "    cold start reference: ~630s to ready (~400s of it weight loading)"
deadline=$(( $(date +%s) + GXMAX_READY_TIMEOUT ))
while :; do
  if gxmax_healthy; then
    log "=== gx-max READY on http://127.0.0.1:${GXMAX_PORT}/v1 ==="
    exit 0
  fi
  if ! gxmax_rank0_running; then
    log "rank0 exited. Last 40 log lines:"; docker logs --tail 40 "${GXMAX_RANK0_NAME}" 2>&1 | tail -40 >&2
    exit 3
  fi
  if ! gxmax_rank1_running; then
    log "rank1 exited. Last 40 log lines:"; n2 "docker logs --tail 40 ${GXMAX_RANK1_NAME} 2>&1 | tail -40" >&2
    exit 3
  fi
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    log "TIMEOUT after ${GXMAX_READY_TIMEOUT}s; ranks still running but not healthy."
    exit 2
  fi
  sleep 10
done
