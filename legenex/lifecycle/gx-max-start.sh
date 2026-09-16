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
# Exit codes: 0 ready | 1 preflight/admission failure | 2 startup timeout
#             3 rank died | 4 node1 safety abort | 5 node2 unreachable
#             130 interrupted.  Every non-zero exit AFTER a
#             rank has been launched runs gx-max-unwind.sh on both nodes.
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"

# shellcheck source=./gx-max-safety.sh
source "${here}/gx-max-safety.sh"

# gx-max admission is the CLUSTER-TAKEOVER policy (D-025), not the ordinary
# `estimate + 30 GiB reserve` formula. The old formula compared the measured
# ~117 GiB load PEAK plus a 30 GiB reserve against a 121.63 GiB node, which
# can never pass: gx-max was refused by arithmetic, not by the cluster state.
# The ordinary formula is unchanged for every single-node tier.
FORCE_DRAIN="${GXMAX_FORCE_DRAIN:-0}"

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

# --------------------------------------------- cluster-takeover admission --
# One hard, non-bypassable check per node before ANY docker run (D-025):
#   * no other large/exclusive resident in the node's ledger
#   * /swapfile-sglang active and >= GXMAX_MIN_SWAP_FREE_GIB swap free
#   * MemAvailable >= GXMAX_CLEAN_START_MIN_AVAIL_GIB (i.e. really drained)
#   * no pre-existing memory pressure (PSI)
#   * management plane healthy on both nodes
# The decision itself lives in gx_orchestrator.resource_guard (one formula,
# used by bash and Python alike). It is re-evaluated under each node's lock
# immediately before that node's rank is launched.
log "=== cluster-takeover admission (node1 + node2) ==="

mgmt_healthy_n1() {
  systemctl is-active --quiet NetworkManager || return 1
  tailscale status --peers=false >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1 || return 1
}
mgmt_healthy_n1 || die "node1 management plane unhealthy (NetworkManager/tailscale/docker)"
n2 "systemctl is-active --quiet NetworkManager && tailscale status --peers=false >/dev/null 2>&1 && docker info >/dev/null 2>&1" \
  || die "node2 management plane unhealthy (NetworkManager/tailscale/docker)"
log "management plane healthy on both nodes"

facts_n1="$(gxs_clean_start_facts "${GXMAX_REQUIRED_SWAPFILE}")"
facts_n2="$(n2 "$(declare -f _gxs_read gxs_clean_start_facts); gxs_clean_start_facts '${GXMAX_REQUIRED_SWAPFILE}'")" \
  || die "could not read node2 clean-start facts"
log "node1 facts: ${facts_n1}"
log "node2 facts: ${facts_n2}"

if guard_n1_result="$(gx_guard_takeover_check node1 gx-max-rank0 "${facts_n1}")"; then
  log "node1 admission: ${guard_n1_result}"
else
  die "node1 admission REFUSED gx-max-rank0: ${guard_n1_result} -- hard refusal, cannot be bypassed"
fi
if guard_n2_result="$(gx_guard_takeover_check node2 gx-max-rank1 "${facts_n2}")"; then
  log "node2 admission: ${guard_n2_result}"
else
  die "node2 admission REFUSED gx-max-rank1: ${guard_n2_result} -- hard refusal, cannot be bypassed"
fi
log "admission: both nodes admitted the gx-max takeover"

if [ "${FORCE_DRAIN}" = "1" ]; then
  log "NOTE: GXMAX_FORCE_DRAIN=1 is set but does not bypass admission. It has no effect."
fi

# ------------------------------------------------------------------- start --
mapfile -t DFLAGS < <(gxmax_docker_flags)
mapfile -t EFLAGS < <(gxmax_env_flags)

# Arm node 1's safety counters BEFORE anything is launched, so a kernel OOM
# kill or driver allocation failure anywhere in the launch window is seen.
gxs_arm

log "=== starting rank1 on node2 ==="
r1_cmd=$(printf '%q ' docker run -d --name "${GXMAX_RANK1_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 1))
# node 2 has its own flock (~/.gx-guard/node2.lock). The admission facts are
# re-read and re-judged on node 2 atomically with that lock (B-019), using
# thresholds rendered into literals here -- node 2's shell has none of our
# variables. This is the clean-start check, not a peak+reserve check.
r1_min_avail_mib=$(( GXMAX_CLEAN_START_MIN_AVAIL_GIB * 1024 ))
r1_min_swap_mib=$(( GXMAX_MIN_SWAP_FREE_GIB * 1024 ))
r1_final_check="$(declare -f _gxs_read gxs_clean_start_facts); f=\$(gxs_clean_start_facts '${GXMAX_REQUIRED_SWAPFILE}'); set -- \$f; a=\${1#*=}; sf=\${2#*=}; act=\${4#*=}; if [ \"\$act\" != 1 ] || [ \"\$a\" -lt ${r1_min_avail_mib} ] || [ \"\$sf\" -lt ${r1_min_swap_mib} ]; then echo \"REFUSED under node2 lock: \$f\" >&2; exit 9; fi"
r1_locked_cmd="mkdir -p \$HOME/.gx-guard && exec 9>\$HOME/.gx-guard/node2.lock && flock -x -w ${GX_GUARD_LOCK_TIMEOUT} 9 && (${r1_final_check}) && ${r1_cmd}"
LAUNCHED=1   # from here on, every failure path must unwind BOTH nodes
n2 "${r1_locked_cmd}" >/dev/null || die "failed to start rank1 (node2 lock busy, clean-start re-check failed, or launch failed)"
n2 "docker logs -f ${GXMAX_RANK1_NAME} > \$HOME/gx-max-rank1.log 2>&1 &" >/dev/null 2>&1 || true

# ---------------------------------------------------- node-2-side deadman --
# rank1-deadman.sh runs ON node 2 with the same gx-max-safety.sh rules, so
# node 2 protects itself even when node 1 cannot reach it (B-020). Armed
# BEFORE rank0 starts, so it also covers rank0 never coming up.
n2 "mkdir -p ~/.gx-guard" >/dev/null 2>&1 || true
if scp -q -o BatchMode=yes -o ConnectTimeout=10 "${here}/rank1-deadman.sh" "${here}/gx-max-safety.sh" \
       "${GXMAX_NODE2_SSH}:.gx-guard/" >/dev/null 2>&1; then
  n2 "chmod +x ~/.gx-guard/rank1-deadman.sh; \
      $(for v in GXMAX_CRIT_AVAIL_MIB GXMAX_CRIT_SWAPFREE_MIB GXMAX_EXHAUST_SUSTAIN_S GXMAX_THRASH_PSI_FULL GXMAX_THRASH_SWAPIN_PPS GXMAX_THRASH_SUSTAIN_S GXMAX_FORK_MAX_MS GXMAX_MGMT_SUSTAIN_S GXMAX_STEADY_FLOOR_GIB GXMAX_STEADY_SUSTAIN_S; do printf '%s=%q ' "$v" "${!v}"; done) \
      setsid nohup ~/.gx-guard/rank1-deadman.sh '${GXMAX_DIST_ADDR}' '${GXMAX_RANK1_NAME}' \
        '${GXMAX_DEADMAN_STARTUP_GRACE}' '${GXMAX_DEADMAN_DEATH_GRACE}' '${GXMAX_DEADMAN_POLL}' \
        '${GXMAX_HEALTH_URL_FROM_N2}' \
        >/dev/null 2>&1 < /dev/null &" >/dev/null 2>&1 || true
  sleep 2
  if n2 "test -f ~/.gx-guard/rank1-deadman.pid && kill -0 \$(cat ~/.gx-guard/rank1-deadman.pid)" 2>/dev/null; then
    log "node2 rank1 deadman ARMED (startup grace ${GXMAX_DEADMAN_STARTUP_GRACE}s, death grace ${GXMAX_DEADMAN_DEATH_GRACE}s)"
  elif ! gxmax_rank1_running; then
    # The deadman exits by design when rank1 is gone, so a missing deadman
    # here usually means rank1 itself died at startup.
    log "rank1 exited immediately after launch. Last 40 log lines:"
    n2 "docker logs --tail 40 ${GXMAX_RANK1_NAME} 2>&1 | tail -40" >&2 || true
    exit 3
  else
    die "node2 rank1 deadman did NOT start -- refusing to continue without node-local protection on node2"
  fi
else
  die "could not deploy rank1-deadman.sh to node2 -- refusing to continue without node-local protection on node2"
fi
gx_guard_register node2 gx-max-rank1 exclusive "${GXMAX_STEADY_RESIDENCY_GIB}" "${GXMAX_RANK1_NAME}" || true
log "rank1 started"

sleep 5

log "=== starting rank0 on node1 ==="
# Under node 1's flock: re-read facts, re-judge, launch, register -- one
# critical section, so no other sanctioned launch can race into node 1.
exec {n1_lock_fd}>"${GX_GUARD_STATE_DIR}/node1.lock"
flock -x -w "${GX_GUARD_LOCK_TIMEOUT}" "${n1_lock_fd}" || die "node1 lock busy (another launch in progress)"
facts_n1="$(gxs_clean_start_facts "${GXMAX_REQUIRED_SWAPFILE}")"
guard_n1_result="$(gx_guard_takeover_check node1 gx-max-rank0 "${facts_n1}")" \
  || die "node1 admission REFUSED under lock: ${guard_n1_result}"
docker run -d --name "${GXMAX_RANK0_NAME}" --restart no \
  "${DFLAGS[@]}" "${EFLAGS[@]}" "${GXMAX_IMAGE}" $(gxmax_args 0) >/dev/null \
  || die "failed to start rank0"
gx_guard_register node1 gx-max-rank0 exclusive "${GXMAX_STEADY_RESIDENCY_GIB}" "${GXMAX_RANK0_NAME}" || true
eval "exec ${n1_lock_fd}>&-"
( docker logs -f "${GXMAX_RANK0_NAME}" > "${GXMAX_LOG_DIR}/gx-max-rank0.log" 2>&1 & ) || true
log "rank0 started"

# ------------------------------------------------------------- wait healthy --
# The wait loop is the node-1 LIVE SAFETY SENTINEL (gx-max-safety.sh):
#   * a rank dies                        -> exit 3
#   * readiness never arrives            -> exit 2
#   * sustained distress / OOM / NV OOM  -> exit 4
#   * node 2 unreachable for too long    -> exit 5
# Each exit runs the two-node unwind via the EXIT trap. A single low
# MemAvailable sample is NOT an abort: the verified launch touched ~1 GiB.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MEMLOG="${GXMAX_LOG_DIR}/gx-max-safety-node1-${STAMP}.tsv"
N2LOG="${GXMAX_LOG_DIR}/gx-max-mem-node2-${STAMP}.tsv"
gxs_header > "${MEMLOG}"
printf 'epoch\tavail_mib\tswap_used_mib\tpsi_full10\n' > "${N2LOG}"
log "node1 safety samples -> ${MEMLOG}; node2 samples -> ${N2LOG} (node2 also logs ~/gx-max-node2-mem.tsv locally)"

min_n2=999999; max_swap2=0; n2_last_sample=0; n2_last_ok=$(date +%s)
N2_SAMPLE_EVERY="${GXMAX_N2_SAMPLE_EVERY:-15}"
t_start=$(date +%s)

n2_sample() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 "${GXMAX_NODE2_SSH}" \
    "awk '/^MemAvailable:/{a=\$2} /^SwapFree:/{f=\$2} /^SwapTotal:/{t=\$2} END{printf \"%d %d\", a/1024, (t-f)/1024}' /proc/meminfo; awk '/^full/{split(\$2,x,\"=\"); printf \" %d\n\", x[2]}' /proc/pressure/memory" 2>/dev/null
}

log "=== waiting for gx-max to become healthy (timeout ${GXMAX_READY_TIMEOUT}s) ==="
log "    cold start reference: ~630s to ready (~400s of it weight loading)"
deadline=$(( t_start + GXMAX_READY_TIMEOUT ))
while :; do
  now=$(date +%s)
  gxs_tick load
  printf '%s\n' "${GXS_SAMPLE}" >> "${MEMLOG}"

  if [ $(( now - n2_last_sample )) -ge "${N2_SAMPLE_EVERY}" ]; then
    n2_last_sample="${now}"
    if read -r a2 s2 p2 < <(n2_sample) && [ -n "${a2:-}" ]; then
      n2_last_ok="${now}"
      printf '%s\t%s\t%s\t%s\n' "${now}" "${a2}" "${s2}" "${p2}" >> "${N2LOG}"
      [ "${a2}" -lt "${min_n2}" ] && min_n2="${a2}"
      [ "${s2}" -gt "${max_swap2}" ] && max_swap2="${s2}"
    fi
  fi

  if gxmax_healthy; then
    t_ready=$(date +%s)
    log "=== gx-max READY on http://127.0.0.1:${GXMAX_PORT}/v1 after $(( t_ready - t_start ))s ==="
    log "    LOAD-phase minima : node1=${GXS_MIN_AVAIL_MIB}MiB node2=${min_n2}MiB MemAvailable"
    log "    LOAD-phase swap   : node1 peak=${GXS_MAX_SWAPUSED_MIB}MiB node2 peak=${max_swap2}MiB; node1 peak PSI full=${GXS_MAX_PSI_FULL10}%; node1 soft NV_ERR lines=${GXS_NV_SOFT:-0}"
    sleep 30   # let loader staging be released before measuring steady state
    gxs_tick steady
    printf '%s\n' "${GXS_SAMPLE}" >> "${MEMLOG}"
    read -r a2 s2 p2 < <(n2_sample) || true
    log "    STEADY-STATE      : node1 avail=${GXS_AVAIL_MIB}MiB swap_used=$(( GXS_SWAPTOTAL_MIB - GXS_SWAPFREE_MIB ))MiB | node2 avail=${a2:-?}MiB swap_used=${s2:-?}MiB"
    printf 'SUMMARY\tstartup_s=%s\tn1_min_avail_mib=%s\tn2_min_avail_mib=%s\tn1_max_swap_mib=%s\tn2_max_swap_mib=%s\tn1_steady_avail_mib=%s\tn2_steady_avail_mib=%s\n' \
      "$(( t_ready - t_start ))" "${GXS_MIN_AVAIL_MIB}" "${min_n2}" "${GXS_MAX_SWAPUSED_MIB}" "${max_swap2}" "${GXS_AVAIL_MIB}" "${a2:-?}" >> "${MEMLOG}"
    # Steady-state protection on node 1 for the rest of the engine's life
    # (node 2 already has rank1-deadman.sh).
    setsid nohup bash "${here}/rank0-watch.sh" >/dev/null 2>&1 < /dev/null &
    log "    node1 rank0-watch armed (log: ${GXMAX_LOG_DIR}/gx-max-rank0-watch.log)"
    exit 0
  fi

  if [ "${GXS_VERDICT}" = abort ]; then
    log "SAFETY ABORT (node1): ${GXS_REASON}"
    exit 4
  fi
  if [ $(( now - n2_last_ok )) -ge "${GXMAX_N2_UNREACHABLE_ABORT_S}" ]; then
    log "SAFETY ABORT: node2 unreachable over SSH for $(( now - n2_last_ok ))s (node2 deadman acts locally regardless)"
    exit 5
  fi
  if ! gxmax_rank0_running; then
    log "rank0 exited. Last 40 log lines:"; docker logs --tail 40 "${GXMAX_RANK0_NAME}" 2>&1 | tail -40 >&2
    exit 3
  fi
  # rank1 liveness costs an ssh; only check on node2 sample ticks.
  if [ "${n2_last_sample}" -eq "${now}" ] && ! gxmax_rank1_running; then
    log "rank1 exited. Last 40 log lines:"; n2 "docker logs --tail 40 ${GXMAX_RANK1_NAME} 2>&1 | tail -40; tail -5 ~/gx-max-rank1-deadman.log" >&2 || true
    exit 3
  fi
  if [ "${now}" -ge "${deadline}" ]; then
    log "TIMEOUT after ${GXMAX_READY_TIMEOUT}s; ranks still running but not healthy."
    exit 2
  fi
  sleep "${GXMAX_SENTINEL_POLL}"
done
