#!/usr/bin/env bash
# ============================================================================
# gx-max-start.sh (V4.1) — acquire BOTH nodes and bring up DeepSeek V4.1 Flash
# EXL3 through the Mia kit.
#
# This is a THIN wrapper. The kit (mia-dsv41/start.sh) owns everything about
# the launch: preflight, weights, image, the two containers
# (dsv41-exl3-head on node 1, dsv41-exl3-worker on node 2), and its own
# health wait. What this wrapper adds is the cluster contract:
#
#   1. the resource-guard admission (D-025: gx-max is an exclusive
#      two-node takeover, ~105 GiB resident per node -- it does not use the
#      ordinary estimate+reserve formula),
#   2. the node-2 hold (D-036: gx-music and other tenants stand down),
#   3. the PROFILE ENVIRONMENT OVERLAY from the registry (the orchestrator
#      passes the per-profile values; they must reach the kit verbatim),
#   4. the marker lines gx_orchestrator.lifecycle parses for its phase
#      tracking (preflight / overlay / loading / warming / ready / unwinding),
#   5. a readiness check that verifies the SERVED MODEL ID, not just /health,
#   6. the two-node unwind on any failure after a container is launched.
#
# Exit codes: 0 ready | 1 preflight/admission failure | 2 startup timeout
#             3 a container died | 4 node1 safety abort | 5 node2 unreachable
#             130 interrupted.
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"
# shellcheck source=./gx-max-safety.sh
source "${here}/gx-max-safety.sh"
# shellcheck source=./node2-holds.sh
source "${here}/node2-holds.sh"

# ---------------------------------------------------------------- preflight --
log "=== gx-max preflight ==="

[ -x "${GXMAX_MIA_DIR}/start.sh" ] || die "Mia kit start.sh not found at ${GXMAX_MIA_DIR}/start.sh (set GXMAX_MIA_DIR)"

if gxmax_ready; then
  log "gx-max is ALREADY serving ${GXMAX_SERVED_MODEL_ID} on :${GXMAX_PORT}. Nothing to do."
  exit 0
fi

# Management plane must be alive on both nodes before a takeover.
mgmt_healthy_n1() {
  systemctl is-active --quiet NetworkManager || return 1
  tailscale status --peers=false >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1 || return 1
}
mgmt_healthy_n1 || die "node1 management plane unhealthy (NetworkManager/tailscale/docker)"
n2 "systemctl is-active --quiet NetworkManager && tailscale status --peers=false >/dev/null 2>&1 && docker info >/dev/null 2>&1" \
  || die "node2 management plane unhealthy (NetworkManager/tailscale/docker)"

DRAIN_SH="$(cd "${here}/../.." >/dev/null 2>&1 && pwd)/ops/dsv41-prestart-drain.sh"
if [ -x "$DRAIN_SH" ]; then
  log "=== pre-start drain (graphical/RDP + non-critical consumers) ==="
  bash "$DRAIN_SH" || die "pre-start drain failed; MemAvailable too low for GPU_MEM_UTIL=0.88"
else
  log "WARN: drain script missing at ${DRAIN_SH}"
fi

# ---------------------------------------------------- admission (D-025) ----
# One hard, non-bypassable check per node before anything is launched. The
# decision itself lives in gx_orchestrator.resource_guard (one formula, used
# by bash and Python alike).
log "=== cluster-takeover admission (node1 + node2) ==="
facts_n1="$(gxs_clean_start_facts "${GXMAX_REQUIRED_SWAPFILE}")" \
  || die "could not read node1 clean-start facts"
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

# ---------------------------------------------------- node-2 hold (D-036) --
# gx-music refuses new engine loads from this moment and queued music jobs
# wait with an explicit gx-max reason.
gx_n2_hold_set gxmax || die "could not set the gx-max hold on node2"
HOLD_SET=1

# ------------------------------------------------- the profile env overlay --
# Everything the registry decided (the orchestrator passes the real values;
# gx-max.conf carries the 'balanced' defaults for a manual run) is logged
# here and exported for the kit below.
log "=== profile overlay ==="
gxmax_profile_env | while IFS= read -r line; do log "  ${line}"; done

# ------------------------------------------------------------- failure path --
# From the moment the kit may have launched a container, any failure must
# unwind BOTH nodes (B-020: an abandoned rank-1 once held ~90 GiB of node 2
# for 80 minutes because nothing on the failure path stopped it).
LAUNCHED=0
_unwind_done=0
on_exit() {
  local rc=$?
  if [ "${LAUNCHED}" -eq 1 ] && [ "${rc}" -ne 0 ] && [ "${_unwind_done}" -eq 0 ]; then
    _unwind_done=1
    log "start failed (exit ${rc}) with a container launched -- running the two-node unwind"
    bash "${here}/gx-max-unwind.sh" --reason "gx-max-start.sh exit ${rc}" \
      || log "!!! UNWIND REPORTED PROBLEMS -- read its FAIL lines above; a container may still be resident"
  fi
  if [ "${HOLD_SET:-0}" -eq 1 ] && [ "${rc}" -ne 0 ]; then
    gx_n2_hold_clear gxmax || log "WARN: could not clear the gx-max hold on node2"
  fi
  exit "${rc}"
}
trap on_exit EXIT
trap 'log "received SIGINT/SIGTERM; aborting"; exit 130' INT TERM

# ------------------------------------------------------------------- start --
log "=== starting the Mia kit (${GXMAX_RANK0_NAME} + ${GXMAX_RANK1_NAME}) ==="
LAUNCHED=1   # the kit launches both containers itself; assume resident from here
gxs_arm
# --- per-rank RoCE v2 GID resolution ----------------------------------------
# The GID *index* of a rank's fabric IPv4 is assigned by the kernel and is NOT
# stable: after a link flap the worker's rocep1s0f0 table re-populated with the
# RoCE v2 entry at index 4 (index 3 an empty hole), while the head's stayed at
# 3. A single fixed NCCL_IB_GID_INDEX then fails the kit's preflight ("set
# NCCL_IB_GID_INDEX ...") or hangs QP setup. So: keep the preferred index
# (NCCL_IB_GID_INDEX, default 3) only where it is a populated RoCE v2 entry for
# that rank's fabric IP; otherwise pick the index that is. Read-only sysfs.
_mia_env() { sed -n "s/^$1=//p" "${GXMAX_MIA_DIR}/.env" 2>/dev/null | tail -1; }
_roce_v2_gid_index() {  # <ib-dev> <ipv4> <preferred-index>; prints the index or nothing
  local dev="$1" ip="$2" want="$3" base hex i g t
  base="/sys/class/infiniband/${dev}/ports/1"
  hex="$(printf '%02x%02x:%02x%02x' ${ip//./ })"
  for i in "${want}" 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    g="$(cat "${base}/gids/${i}" 2>/dev/null)" || continue
    t="$(cat "${base}/gid_attrs/types/${i}" 2>/dev/null)" || continue
    [ "${t}" = "RoCE v2" ] && [ "${g##*:ffff:}" = "${hex}" ] && { echo "${i}"; return 0; }
  done
  return 1
}
_want_gid="${NCCL_IB_GID_INDEX:-3}"
_head_ib="$(_mia_env HEAD_CX7_IB)";  _head_ib="${_head_ib:-rocep1s0f1}"
_work_ib="$(_mia_env WORKER_CX7_IB)"; _work_ib="${_work_ib:-rocep1s0f0}"
_head_ip="$(_mia_env HEAD_IP)"; _work_ip="$(_mia_env WORKER_IP)"
HEAD_GID_RESOLVED="$(_roce_v2_gid_index "${_head_ib}" "${_head_ip}" "${_want_gid}")" \
  || die "no RoCE v2 GID for ${_head_ip} on head ${_head_ib} (GID table empty or link down)"
WORKER_GID_RESOLVED="$(n2 "$(declare -f _roce_v2_gid_index); _roce_v2_gid_index '${_work_ib}' '${_work_ip}' '${_want_gid}'")" \
  || die "no RoCE v2 GID for ${_work_ip} on worker ${_work_ib} (GID table empty or link down)"
log "NCCL GID index: head ${_head_ib}=${HEAD_GID_RESOLVED} worker ${_work_ib}=${WORKER_GID_RESOLVED} (preferred ${_want_gid})"

KIT_ENV=(SERVED_MODEL_NAME="${SERVED_MODEL_NAME}"
         HEAD_GID="${HEAD_GID_RESOLVED}"
         WORKER_GID="${WORKER_GID_RESOLVED}"
         MAX_NUM_SEQS="${MAX_NUM_SEQS}"
         SPEC_METHOD="${SPEC_METHOD}"
         MAX_MODEL_LEN="${MAX_MODEL_LEN}")
[ -n "${DSPARK_TOKENS:-}" ] && KIT_ENV+=(DSPARK_TOKENS="${DSPARK_TOKENS}")
set +e
( cd "${GXMAX_MIA_DIR}" && env "${KIT_ENV[@]}" bash ./start.sh )
kit_rc=$?
set -e
if [ "${kit_rc}" -ne 0 ]; then
  log "the Mia kit start.sh exited ${kit_rc} -- its own logs above say why"
  exit 3
fi

# ------------------------------------------------------------- wait healthy --
# The kit waits for its own /health; READY here is the stronger fact: the
# served model id verified against /v1/models. The orchestrator layers a real
# completion probe on top of this (lifecycle.py).
log "=== waiting for the model to become healthy (timeout ${GXMAX_READY_TIMEOUT}s, id ${GXMAX_SERVED_MODEL_ID}) ==="
t_start=$(date +%s)
deadline=$(( t_start + GXMAX_READY_TIMEOUT ))
last_containers_ok=0
while :; do
  now=$(date +%s)
  if gxmax_ready; then
    t_ready=$(date +%s)
    log "=== gx-max READY on http://127.0.0.1:${GXMAX_PORT}/v1 after $(( t_ready - t_start ))s ==="
    log "    MemAvailable at ready: node1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)GiB node2=$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo")GiB"
    # Record the steady-state residency in the resource-guard ledger so the
    # admission math for every other workload sees the truth (D-025).
    gx_guard_register node1 gx-max-rank0 exclusive "${GXMAX_STEADY_RESIDENCY_GIB}" "${GXMAX_RANK0_NAME}" || true
    gx_guard_register node2 gx-max-rank1 exclusive "${GXMAX_STEADY_RESIDENCY_GIB}" "${GXMAX_RANK1_NAME}" || true
    # Steady-state protection on node 1 for the rest of the engine's life
    # (node 2 has rank1-deadman.sh via the kit's own memguard policy).
    setsid nohup bash "${here}/rank0-watch.sh" >/dev/null 2>&1 < /dev/null &
    log "    node1 rank0-watch armed (log: ${GXMAX_LOG_DIR}/gx-max-rank0-watch.log)"
    exit 0
  fi

  if ! gxmax_rank0_running; then
    log "the head container exited. Last 40 log lines:"
    docker logs --tail 40 "${GXMAX_RANK0_NAME}" 2>&1 | tail -40 >&2 || true
    exit 3
  fi
  if [ "${now}" -ge "${deadline}" ]; then
    log "TIMEOUT after ${GXMAX_READY_TIMEOUT}s; containers still running but the model is not serving."
    exit 2
  fi
  if [ $(( now - last_containers_ok )) -ge "${GXMAX_SENTINEL_POLL}" ]; then
    last_containers_ok="${now}"
    if ! gxmax_rank1_running; then
      log "the worker container exited on node2. Last 40 log lines:"
      n2 "docker logs --tail 40 ${GXMAX_RANK1_NAME} 2>&1 | tail -40" >&2 || true
      exit 3
    fi
    n2 true 2>/dev/null || { log "SAFETY ABORT: node2 unreachable over SSH"; exit 5; }
  fi
  sleep "${GXMAX_SENTINEL_POLL}"
done
