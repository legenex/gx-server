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
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"

FORCE_DRAIN="${GXMAX_FORCE_DRAIN:-0}"
# Estimated whole-node footprint of one SGLang rank, used by the hard
# admission guard below. See gx_orchestrator.resource_guard.WORKLOAD_SIZING.
GXMAX_RANK_ESTIMATED_GIB="${GXMAX_RANK_ESTIMATED_GIB:-90}"

# Containers that must not hold GPU/unified memory while gx-max runs.
# NOTE 2026-09-15: CONFLICTS_N2 previously listed "comfyui" and
# "llama-swap-node02", which do not match any real container name on node 2
# (the actual names are gx-comfyui and gx-llama-swap-node02, per
# docker-compose.media.yml/docker-compose.node02.yml) -- `docker inspect` on
# a nonexistent name just falls through to "not running" in drain_node2(), so
# this was silently draining nothing. Found and fixed the first time the
# media stack was actually deployed and running when this script was read
# closely. Also added gx-media-router: stopping gx-comfyui out from under it
# without stopping it too would leave it up but broken.
CONFLICTS_N1=(gx-mini gx-fast vllm llama-swap-node01)
CONFLICTS_N2=(gx-reason gx-comfyui gx-media-router gx-llama-swap-node02)

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
#
# NOT `ping`: found 2026-09-15, the first time this script was ever actually
# invoked through the real orchestrator (gx_orchestrator.server, systemd unit
# with NoNewPrivileges=true) rather than run directly from an interactive
# shell. `ping` needs a raw ICMP socket, normally granted via the binary's
# cap_net_raw file capability -- but NoNewPrivileges blocks a process from
# gaining ANY capability via exec, file capabilities included, and this
# host's net.ipv4.ping_group_range is empty so there is no unprivileged
# fallback either. Confirmed empirically: `ping` under NoNewPrivileges=true
# fails immediately with "socket: Operation not permitted", which this
# preflight was silently treating as "rail unreachable" -- so gx-max could
# never succeed via its actual production entry point, only via a direct
# shell invocation. A plain TCP connect attempt needs no special capability
# and works identically either way: a fast "Connection refused" proves the
# kernel on the far end answered (nothing needs to be listening on the probe
# port), while a real timeout means genuinely unreachable.
fabric_rail_reachable() {
  local peer="$1" out rc
  out=$(timeout 2 bash -c "exec 3<>/dev/tcp/${peer}/1" 2>&1); rc=$?
  [ "${rc}" -eq 0 ] && return 0
  printf '%s' "${out}" | grep -qi 'connection refused'
}
for peer in 192.168.100.11 192.168.101.11; do
  fabric_rail_reachable "${peer}" || die "ConnectX rail unreachable: ${peer}"
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

# ------------------------------------------------------ hard admission guard --
# One hard, non-bypassable safety check per node before ANY docker run is
# attempted (2026-09-14 hardening -- see coordination/BLOCKERS.md B-012: node
# 2 was wedged by a second ~77GB model landing on top of gx-reason). This
# reuses the EXACT SAME arithmetic gx-safe-run.sh and the Python orchestrator
# use (gx_orchestrator.resource_guard.compute_admission) -- there is one
# formula, not a second one duplicated in bash.
#
# GXMAX_FORCE_DRAIN no longer has any effect on this check: a hard guard that
# an env var can switch off is not a hard guard. It is read below only to
# emit a note if someone still sets it, so existing callers do not silently
# think they bypassed something.
log "=== hard admission guard (node1 + node2, reserve=${GX_GUARD_RESERVE_GIB}GiB) ==="
guard_meminfo_n2="$(mktemp)"
trap 'rm -f "${guard_meminfo_n2}"' EXIT
n2 "cat /proc/meminfo" > "${guard_meminfo_n2}" || die "could not read node2 /proc/meminfo for the admission guard"

if guard_n1_result="$(gx_guard_check node1 gx-max-rank0 exclusive "${GXMAX_RANK_ESTIMATED_GIB}")"; then
  log "node1 admission: ${guard_n1_result}"
else
  die "node1 admission guard REFUSED gx-max-rank0: ${guard_n1_result} -- hard refusal, cannot be bypassed"
fi
if guard_n2_result="$(GX_GUARD_MEMINFO="${guard_meminfo_n2}" gx_guard_check node2 gx-max-rank1 exclusive "${GXMAX_RANK_ESTIMATED_GIB}")"; then
  log "node2 admission: ${guard_n2_result}"
else
  die "node2 admission guard REFUSED gx-max-rank1: ${guard_n2_result} -- hard refusal, cannot be bypassed"
fi
log "admission guard: both nodes admitted gx-max"

if [ "${FORCE_DRAIN}" = "1" ]; then
  log "NOTE: GXMAX_FORCE_DRAIN=1 is set but no longer bypasses the hard admission guard above. It has no effect in this script any more."
fi

# ------------------------------------------------------------------- start --
mapfile -t DFLAGS < <(gxmax_docker_flags)
mapfile -t EFLAGS < <(gxmax_env_flags)

log "=== starting rank1 on node2 ==="
# Build the remote command as a single properly quoted string.
r1_cmd=$(printf '%q ' docker run -d --name "${GXMAX_RANK1_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 1))
# Wrap in a remote flock so a concurrent large-workload launch against node2
# cannot race past this point. Lock path is a documented convention
# (~/.gx-guard/node2.lock) that any future node2-side tooling should reuse --
# node2 has no deployed copy of resource-guard.sh yet, so this is the one
# piece of cross-process protection it gets: a real flock, not yet backed
# by the full residency ledger.
#
# TOCTOU note (found 2026-09-15 by independent review, coordination/
# BLOCKERS.md B-019): the admission check above and this launch are not
# fully atomic -- the check runs unlocked, against a meminfo snapshot taken
# moments earlier, and only the launch itself is lock-guarded. A full fix
# means moving the admission arithmetic inside this same held lock, which
# needs the ledger deployed to node2 (a bigger change, not done here). As a
# narrower, low-risk mitigation: re-check raw MemAvailable one more time,
# atomically with the lock, immediately before the docker run -- this
# closes the most dangerous part of the window (something else consuming
# node2's memory between the check above and the lock acquired here) even
# though it re-validates raw headroom rather than the full ledger-aware
# admission decision.
# Threshold is computed HERE, on node1, into a literal number -- the
# remote command below runs on node2's own shell, which has never heard of
# $GXMAX_RANK_ESTIMATED_GIB/$GX_GUARD_RESERVE_GIB and would silently treat
# them as 0 in arithmetic context (making the check always pass) if left
# as variable references instead of an already-computed literal.
r1_min_avail_gib=$((GXMAX_RANK_ESTIMATED_GIB + GX_GUARD_RESERVE_GIB))
r1_final_check="avail=\$(awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo); if [ \"\${avail:-0}\" -lt ${r1_min_avail_gib} ]; then echo \"REFUSED: node2 MemAvailable=\${avail}GiB, need >= ${r1_min_avail_gib}GiB (re-checked atomically with the lock)\" >&2; exit 9; fi"
r1_locked_cmd="mkdir -p \$HOME/.gx-guard && exec 9>\$HOME/.gx-guard/node2.lock && flock -x -w ${GX_GUARD_LOCK_TIMEOUT} 9 && (${r1_final_check}) && ${r1_cmd}"
n2 "${r1_locked_cmd}" >/dev/null || die "failed to start rank1 (node2 lock busy, final memory re-check failed, or launch failed)"
n2 "docker logs -f ${GXMAX_RANK1_NAME} > \$HOME/gx-max-rank1.log 2>&1 &" >/dev/null 2>&1 || true
# Best-effort local bookkeeping: node1 has no authoritative view of node2's
# residency (no ledger runs there), but recording this here means a later
# gx-max-stop.sh release, or a `resource-guard.sh status node2` check run
# from node1, at least reflects what THIS script believes it started.
gx_guard_register node2 gx-max-rank1 exclusive "${GXMAX_RANK_ESTIMATED_GIB}" "${GXMAX_RANK1_NAME}" || true
log "rank1 started"

sleep 5

log "=== starting rank0 on node1 ==="
# Routed through gx_guard_run: this re-validates admission (holding node1's
# lock for the whole launch) at the exact moment of the real docker run, and
# registers residency on success -- the same sanctioned path gx-safe-run.sh
# uses for any other large/exclusive container on this node.
gx_guard_run node1 gx-max-rank0 exclusive "${GXMAX_RANK_ESTIMATED_GIB}" -- \
  docker run -d --name "${GXMAX_RANK0_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 0) \
  || die "failed to start rank0 (admission refused, node1 lock busy, or launch failed)"
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
