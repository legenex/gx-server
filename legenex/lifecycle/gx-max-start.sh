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
#             4 memory tripwire | 130 interrupted.  Every non-zero exit AFTER a
#             rank has been launched runs gx-max-unwind.sh on both nodes.
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"

# gx-max uses the SAME 30 GiB reserve floor as every other tier. The
# 2026-09-15 B-017 "option 1" exception (a 5 GiB gx-max-only reserve) was
# revoked by the operator on 2026-09-16: the floor is a requirement, and the
# way gx-max is made to fit inside it is ENGINE TUNING, not a smaller floor.
# See gx-max.conf's reserve + runtime-tuning blocks and DECISIONS.md D-022.
GX_GUARD_RESERVE_GIB="${GXMAX_GUARD_RESERVE_GIB}"

FORCE_DRAIN="${GXMAX_FORCE_DRAIN:-0}"
# Estimated whole-node footprint of one SGLang rank, used by the hard
# admission guard below. See gx_orchestrator.resource_guard.WORKLOAD_SIZING.
#
# History: 90 (initial) -> 95 (2026-09-15, after the B-020 OOM) -> the value
# below (2026-09-16). The 95 GiB figure described the UNTUNED engine
# (--mem-fraction-static 0.80). It is not an estimate that can ever be
# admitted against a 30 GiB reserve: 95 + 30 = 125 GiB on a 121.63 GiB node.
# That arithmetic is the whole reason the engine had to be retuned rather
# than the floor lowered.
#
# The value below is the measured LOAD-PHASE PEAK, not the steady state, and
# that choice is deliberate. A launch has to survive its peak; sizing
# admission from the steady state is how the 2026-09-15 attempt got admitted
# at "20 GiB of nominal slack" and was then OOM-killed anyway (B-020).
#
# 117 GiB is what eight instrumented two-node runs on 2026-09-16 measured:
# loading one rank takes a 121.63 GiB node from ~110 GiB MemAvailable to
# between 437 MiB and 0 MiB, on BOTH nodes, and nothing changes it --
# --mem-fraction-static (0.50/0.70), --context-length (327680/65536/32768),
# --chunked-prefill-size, --cuda-graph-max-bs-decode, --max-running-requests,
# the container --memory cap (106g/98g/32g/28g) and --load-format
# (auto/layered/runai_streamer) were each tested. See B-022 and D-022.
#
# The consequence is intended and is the correct behaviour: 117 + any reserve
# exceeds a 121.63 GiB node, so this guard REFUSES gx-max instead of starting
# a launch that measurement says will end in a kernel OOM kill. That refusal
# is the honest state of the tier, and lifting it needs a human decision about
# a LOCKED constraint (the model, the quantisation, or the node count), not a
# smaller number here.
GXMAX_RANK_ESTIMATED_GIB="${GXMAX_RANK_ESTIMATED_GIB:-117}"

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
#
# NOTE 2026-09-16: CONFLICTS_N1 had the SAME dead-name bug the node-2 list was
# fixed for a day earlier -- "vllm" and "llama-swap-node01" match no container
# on node 1. The real names are gx-fast (spawned by llama-swap per
# node01.yaml's `docker run --name gx-fast`) and gx-llama-swap-node01 (per
# docker-compose.gateway.yml's container_name). `docker inspect` on a
# nonexistent name falls through to "not running", so node 1's llama-swap
# control plane was never actually drained before a gx-max run -- it stayed
# up and free to spawn a model into the memory gx-max was about to claim.
CONFLICTS_N1=(gx-mini gx-fast gx-llama-swap-node01)
CONFLICTS_N2=(gx-reason gx-comfyui gx-media-router gx-llama-swap-node02)

# ------------------------------------------------------- FAILURE UNWIND ----
# LAUNCHED tracks whether any rank has actually been started. Until then a
# failure needs no teardown (nothing is resident). From the moment rank1's
# `docker run` is issued, ANY exit path other than a healthy engine MUST
# unwind both nodes -- that is the whole lesson of B-020, where rank0 was
# OOM-killed, this script exited 3, and rank1 was left holding ~90 GiB of
# node 2 for 80 minutes because nothing on the failure path stopped it.
#
# The handler runs from a single EXIT trap so it cannot be missed: not by the
# exit-3 rank-died path, not by the exit-2 timeout path, not by `set -e`
# firing on an unexpected command, and not by SIGINT/SIGTERM (an operator
# hitting Ctrl-C, or the orchestrator's subprocess timeout killing us). It is
# installed BEFORE preflight: when it lived next to the admission guard, its
# own `DRAINED=0` initialiser ran *after* the drain had already set DRAINED=1
# and silently disabled the restore path.
LAUNCHED=0
# DRAINED tracks the other half of the problem: the drain below stops
# gx-mini/gx-fast/llama-swap on BOTH nodes *before* the admission guard runs,
# so a refusal -- or any other failure between the two -- used to leave the
# cluster with no normal service and nothing to bring it back. Found
# 2026-09-16 when the guard started (correctly) refusing gx-max on the real
# measured numbers: every refusal silently took gx-mini and both llama-swaps
# down with it. A launch that never started must put the cluster back exactly
# as it found it.
DRAINED=0
_unwind_done=0
on_exit() {
  local rc=$?
  rm -f "${guard_meminfo_n2:-}" 2>/dev/null || true
  if [ "${LAUNCHED}" -eq 0 ] && [ "${DRAINED}" -eq 1 ] && [ "${rc}" -ne 0 ]; then
    log "start aborted before any rank was launched (exit ${rc}); restoring the normal workloads that were drained"
    "${here}/restore-normal.sh" >/dev/null 2>&1 || log "WARN: restore-normal.sh reported a problem"
  fi
  if [ "${LAUNCHED}" -eq 1 ] && [ "${rc}" -ne 0 ] && [ "${_unwind_done}" -eq 0 ]; then
    _unwind_done=1
    log "!!! gx-max start failed (exit ${rc}) with ranks launched -- running the two-node unwind"
    # Never let the unwind's own exit code replace the real failure code.
    bash "${here}/gx-max-unwind.sh" --reason "gx-max-start.sh exit ${rc}" \
      || log "!!! UNWIND REPORTED PROBLEMS -- read its FAIL lines above; a rank may still be resident"
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'log "received SIGINT/SIGTERM; aborting"; exit 130' INT TERM

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
DRAINED=1
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
LAUNCHED=1   # from here on, every failure path must unwind BOTH nodes
n2 "${r1_locked_cmd}" >/dev/null || die "failed to start rank1 (node2 lock busy, final memory re-check failed, or launch failed)"
n2 "docker logs -f ${GXMAX_RANK1_NAME} > \$HOME/gx-max-rank1.log 2>&1 &" >/dev/null 2>&1 || true

# ---------------------------------------------------- node-2-side deadman --
# Copy and arm rank1-deadman.sh ON node 2. Every remote cleanup path needs a
# healthy node 2 exactly when node 2 is least healthy (B-020: the unwind ssh
# timed out because the orphan it was trying to kill had already starved the
# host). A watchdog that is already resident on node 2 does not have that
# dependency. This is armed BEFORE rank0 is started, so it also covers the
# case where rank0 never comes up at all.
n2 "mkdir -p ~/.gx-guard" >/dev/null 2>&1 || true
if scp -q -o BatchMode=yes -o ConnectTimeout=10 "${here}/rank1-deadman.sh" \
       "${GXMAX_NODE2_SSH}:.gx-guard/rank1-deadman.sh" >/dev/null 2>&1; then
  n2 "chmod +x ~/.gx-guard/rank1-deadman.sh; \
      setsid nohup ~/.gx-guard/rank1-deadman.sh '${GXMAX_DIST_ADDR}' '${GXMAX_RANK1_NAME}' \
        '${GXMAX_DEADMAN_STARTUP_GRACE}' '${GXMAX_DEADMAN_DEATH_GRACE}' '${GXMAX_DEADMAN_POLL}' \
        '${GXMAX_ABORT_FLOOR_GIB}' '' '${GXMAX_LOAD_FLOOR_GIB}' \
        'http://127.0.0.1:${GXMAX_PORT}/health' \
        >/dev/null 2>&1 < /dev/null &" >/dev/null 2>&1 || true
  sleep 2
  if n2 "test -f ~/.gx-guard/rank1-deadman.pid && kill -0 \$(cat ~/.gx-guard/rank1-deadman.pid)" 2>/dev/null; then
    log "node2 rank1 deadman ARMED (startup grace ${GXMAX_DEADMAN_STARTUP_GRACE}s, death grace ${GXMAX_DEADMAN_DEATH_GRACE}s)"
  else
    log "WARN: node2 rank1 deadman did NOT start -- orphan protection falls back to the remote unwind only"
  fi
else
  log "WARN: could not deploy rank1-deadman.sh to node2 -- orphan protection falls back to the remote unwind only"
fi
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
# The wait loop is also the LIVE MEMORY SENTINEL. Three things can end a
# startup badly and all three are handled here rather than being left to the
# kernel:
#   * a rank dies                 -> exit 3, EXIT trap unwinds both nodes
#   * readiness never arrives     -> exit 2, EXIT trap unwinds both nodes
#   * memory crosses the tripwire -> exit 4, EXIT trap unwinds both nodes
#
# The third is new (2026-09-16). On 2026-09-15 nothing watched memory during
# startup: the node ran itself down to 855 MiB free and the kernel's global
# OOM killer made the decision instead, at the worst possible moment (mid
# weight-load, with a live rank on the other node). Aborting at a floor we
# choose is strictly better than being killed at a floor the kernel chooses.
#
# Samples are written to a TSV so the real minima/peaks can be reported
# afterwards instead of estimated. node 2 is sampled less often (it costs an
# ssh); its own deadman enforces the same floor locally and continuously.
MEMLOG="${GXMAX_LOG_DIR}/gx-max-mem-$(date -u +%Y%m%dT%H%M%SZ).tsv"
printf 'epoch\tnode1_avail_gib\tnode1_swap_mib\tnode2_avail_gib\tnode2_swap_mib\tphase\n' > "${MEMLOG}"
log "memory samples -> ${MEMLOG} (load floor ${GXMAX_LOAD_FLOOR_GIB}GiB, steady floor ${GXMAX_ABORT_FLOOR_GIB}GiB, guard reserve ${GX_GUARD_RESERVE_GIB}GiB)"

n1_avail() { awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo; }
n1_swap()  { awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo; }

min_n1=999; min_n2=999; max_swap1=0; max_swap2=0
n2_avail_cached=""; n2_swap_cached=""; n2_last_sample=0
N2_SAMPLE_EVERY="${GXMAX_N2_SAMPLE_EVERY:-15}"

log "=== waiting for gx-max to become healthy (timeout ${GXMAX_READY_TIMEOUT}s) ==="
log "    cold start reference: ~630s to ready (~400s of it weight loading)"
deadline=$(( $(date +%s) + GXMAX_READY_TIMEOUT ))
while :; do
  now=$(date +%s)
  a1=$(n1_avail); s1=$(n1_swap)
  [ "${a1}" -lt "${min_n1}" ] && min_n1="${a1}"
  [ "${s1}" -gt "${max_swap1}" ] && max_swap1="${s1}"

  if [ $(( now - n2_last_sample )) -ge "${N2_SAMPLE_EVERY}" ]; then
    n2_last_sample="${now}"
    if sample=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${GXMAX_NODE2_SSH}" \
          "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo; awk '/SwapTotal/{t=\$2} /SwapFree/{f=\$2} END{print int((t-f)/1024)}' /proc/meminfo" 2>/dev/null); then
      n2_avail_cached=$(printf '%s' "${sample}" | sed -n 1p)
      n2_swap_cached=$(printf '%s' "${sample}" | sed -n 2p)
      if [ -n "${n2_avail_cached}" ]; then
        [ "${n2_avail_cached}" -lt "${min_n2}" ] && min_n2="${n2_avail_cached}"
        [ "${n2_swap_cached:-0}" -gt "${max_swap2}" ] && max_swap2="${n2_swap_cached}"
      fi
    fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${now}" "${a1}" "${s1}" "${n2_avail_cached:-}" "${n2_swap_cached:-}" "starting" >> "${MEMLOG}"

  if gxmax_healthy; then
    log "=== gx-max READY on http://127.0.0.1:${GXMAX_PORT}/v1 ==="
    log "    LOAD-phase minima    : node1=${min_n1}GiB node2=${min_n2}GiB (load floor ${GXMAX_LOAD_FLOOR_GIB}GiB)"
    log "    startup swap peaks   : node1=${max_swap1}MiB node2=${max_swap2}MiB"
    sleep 20   # let the loader's staging memory be released before measuring steady state
    steady1=$(n1_avail)
    steady2=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${GXMAX_NODE2_SSH}" "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>/dev/null || echo "?")
    log "    STEADY-STATE          : node1=${steady1}GiB node2=${steady2}GiB (steady floor ${GXMAX_ABORT_FLOOR_GIB}GiB)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "${steady1}" "${max_swap1}" "${steady2}" "${max_swap2}" "STEADY" >> "${MEMLOG}"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" "${min_n1}" "${max_swap1}" "${min_n2}" "${max_swap2}" "MINIMA" >> "${MEMLOG}"
    exit 0
  fi

  # --- tripwire: abort ourselves rather than be OOM-killed ---
  # LOAD-phase floor only. The steady-state reserve cannot be enforced here:
  # weight loading itself takes both nodes to 1-8 GiB MemAvailable for
  # ~60-120s and no engine setting changes that (see gx-max.conf's
  # GXMAX_LOAD_FLOOR_GIB note). Enforcing the steady floor during load
  # aborted two otherwise-healthy launches before this was measured.
  if [ "${a1}" -lt "${GXMAX_LOAD_FLOOR_GIB}" ]; then
    log "MEMORY ABORT: node1 MemAvailable ${a1}GiB fell below the ${GXMAX_LOAD_FLOOR_GIB}GiB load-phase tripwire"
    exit 4
  fi
  if [ -n "${n2_avail_cached}" ] && [ "${n2_avail_cached}" -lt "${GXMAX_LOAD_FLOOR_GIB}" ]; then
    log "MEMORY ABORT: node2 MemAvailable ${n2_avail_cached}GiB fell below the ${GXMAX_LOAD_FLOOR_GIB}GiB load-phase tripwire"
    exit 4
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
  sleep "${GXMAX_SENTINEL_POLL}"
done
