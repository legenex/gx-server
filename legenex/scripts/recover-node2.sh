#!/usr/bin/env bash
# ==============================================================================
# recover-node2.sh — verify gx10-02 after a power cycle / wedge, before letting
# anything touch it again.
#
# Context (see coordination/BLOCKERS.md B-011/B-012, coordination/DECISIONS.md
# D-009, CURRENT_STATE.md): node 2 can go into sustained mmap thrashing when two
# large models are resident at once. The kernel stays up (ICMP keeps answering)
# but userspace starves -- SSH cannot complete its banner exchange, llama-swap
# stops answering. Recovery from that state requires a physical power cycle.
#
# This script does NOT reboot or power-cycle anything (impossible remotely once
# a node is in that state anyway). It is what a human or the next agent runs
# AFTER the physical power cycle, to answer one question with evidence, not
# assumption: "is it actually safe to let load back onto this node?"
#
# What it checks, in order:
#   1.  SSH reachable                      (ssh legenex-02@gx10-02, NOT the
#                                            fabric address -- CURRENT_STATE.md
#                                            confirms 192.168.100.11 refuses SSH,
#                                            it is a model-traffic-only address)
#   2.  hostname is really gx10-02
#   3.  kernel is EXACTLY 6.17.0-1032-nvidia  (L-4 / D-001 -- 7.0 breaks RDMA
#       memory registration and kills gx-max; this is a hard gate, not advice)
#   4.  NVIDIA driver / nvidia-smi
#   5.  Docker daemon reachable
#   6.  both ConnectX/RoCE rails up (192.168.100.11 + 192.168.101.11)
#   7.  swap still includes /swapfile-sglang at >= 47 GiB (L-8)
#   8.  disk space on /
#   9.  running model processes / stale containers (inventory, not action)
#   10. lifecycle lease staleness (dead PID files, exited containers)
#   11. any giant workload that auto-started unexpectedly
#   12. (only with --apply) clear ONLY the stale items found in 10, never
#       anything under /srv/models or /srv/cache, never a RUNNING container
#   13. node2 service health checks (llama-swap, over both loopback and the
#       fabric address that node 1 actually uses)
#
# This script NEVER starts gx-max or gx-reason. Bringing those up is a decision
# for a human (or legenex/tests/gx-max-validate.sh / a real client request)
# once this report is all PASS, not something recovery does on its own.
#
# Safety:
#   * Default mode is REPORT ONLY. No file is deleted, no container is removed,
#     unless --apply is also given.
#   * Every remote (ssh) call is wrapped in a hard wall-clock timeout via the
#     `timeout` command. `ssh -o ConnectTimeout=N` only bounds the TCP connect
#     phase -- it does NOT bound a stuck banner/protocol exchange, which is
#     exactly the failure mode recorded in B-012 ("TCP connects succeed on
#     ports 22 and 28080 but sshd cannot complete a banner exchange"). Without
#     the outer `timeout`, a still-unhealthy node could hang this script
#     indefinitely; with it, every step fails visibly within a bounded time.
#   * Never touches /srv/models or /srv/cache, under --apply or otherwise.
#   * Idempotent: safe to re-run as many times as needed while triaging.
#
# Usage:
#   legenex/scripts/recover-node2.sh                # report only (default)
#   legenex/scripts/recover-node2.sh --apply         # also clear stale state
#   legenex/scripts/recover-node2.sh --verbose
#
# Exit code = number of FAILed checks (0 = all clear).
# ==============================================================================
set -uo pipefail

# ------------------------------------------------------------------- config --
NODE2_SSH="${GX_NODE2_SSH:-legenex-02@gx10-02}"
NODE2_FABRIC_A="${GX_NODE2_FABRIC_A:-192.168.100.11}"
NODE2_FABRIC_B="${GX_NODE2_FABRIC_B:-192.168.101.11}"
EXPECTED_KERNEL="${GX_EXPECTED_KERNEL:-6.17.0-1032-nvidia}"
SWAPFILE_NAME="${GX_SWAPFILE_NAME:-/swapfile-sglang}"
SWAPFILE_MIN_GIB="${GX_SWAPFILE_MIN_GIB:-47}"
DISK_MIN_FREE_GB="${GX_DISK_MIN_FREE_GB:-20}"
SWAP_PORT="${GX_SWAP_PORT:-28080}"
SSH_CONNECT_TIMEOUT="${GX_SSH_CONNECT_TIMEOUT:-10}"
# Hard wall-clock cap on ANY single remote call. See header note above.
SSH_HARD_TIMEOUT="${GX_SSH_HARD_TIMEOUT:-20}"
LOG_DIR="${GX_LOG_DIR:-/srv/logs}"

# Containers this script is EVER allowed to remove under --apply, and only when
# they are already stopped (Exited/Dead), never running. Nothing else is
# touched, and this list intentionally contains no path under /srv.
SAFE_TO_CLEAR_CONTAINERS=(gx-reason gx-reason-diag-cpu comfyui-diag)

APPLY=0
VERBOSE=0

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --verbose) VERBOSE=1 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

mkdir -p "${LOG_DIR}" 2>/dev/null || LOG_DIR="/tmp"
LOG_FILE="${LOG_DIR}/recover-node2-$(date -u +%Y%m%dT%H%M%SZ).log"

# ------------------------------------------------------------------ logging --
PASS=0; FAIL=0; WARN=0; SKIP=0
declare -a RESULTS=()

ts() { date -Is; }
_write() { printf '%s\n' "$*" | tee -a "${LOG_FILE}" >&2; }
log()      { _write "[$(ts)] INFO  $*"; }
step_pass(){ PASS=$((PASS+1)); _write "[$(ts)] PASS  $1"; RESULTS+=("PASS  $1"); }
step_fail(){ FAIL=$((FAIL+1)); _write "[$(ts)] FAIL  $1 :: ${2:-}"; RESULTS+=("FAIL  $1 :: ${2:-}"); }
step_warn(){ WARN=$((WARN+1)); _write "[$(ts)] WARN  $1 :: ${2:-}"; RESULTS+=("WARN  $1 :: ${2:-}"); }
step_skip(){ SKIP=$((SKIP+1)); _write "[$(ts)] SKIP  $1 :: ${2:-}"; RESULTS+=("SKIP  $1 :: ${2:-}"); }
vlog() { [ "${VERBOSE}" = "1" ] && _write "[$(ts)] ...   $*"; return 0; }

# Hard-bounded remote command. Never hangs past SSH_HARD_TIMEOUT even if the
# SSH banner exchange itself never completes.
remote() {
  timeout "${SSH_HARD_TIMEOUT}" ssh -o BatchMode=yes \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "${NODE2_SSH}" "$@"
}

NODE2_REACHABLE=0
KERNEL_BAD=0

log "=============================================================="
log " recover-node2.sh -- node2 recovery verification"
log " target: ${NODE2_SSH}   apply=${APPLY}   log=${LOG_FILE}"
log "=============================================================="

# --------------------------------------------------------- 1. ssh reachable --
check_ssh() {
  local out rc
  out=$(remote 'true' 2>&1); rc=$?
  if [ "${rc}" -eq 0 ]; then
    step_pass "ssh reachable (${NODE2_SSH})"
    NODE2_REACHABLE=1
  elif [ "${rc}" -eq 124 ]; then
    step_fail "ssh reachable" "no response within ${SSH_HARD_TIMEOUT}s -- this is the exact B-012 symptom (TCP may connect but the banner exchange never completes). Node 2 userspace is likely still starved; do not proceed."
  else
    step_fail "ssh reachable" "ssh exited ${rc}: ${out:0:300}"
  fi
}

# ----------------------------------------------------------- 2. hostname --
check_hostname() {
  local h rc
  h=$(remote hostname 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "hostname" "could not read hostname: ${h:0:200}"
  elif [ "${h}" = "gx10-02" ]; then
    step_pass "hostname is gx10-02"
  else
    step_fail "hostname" "expected gx10-02, got '${h}' -- is GX_NODE2_SSH pointed at the right box?"
  fi
}

# ------------------------------------------------------------- 3. kernel --
check_kernel() {
  local k rc
  k=$(remote uname -r 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "kernel version" "could not read uname -r: ${k:0:200}"
    KERNEL_BAD=1
    return
  fi
  if [ "${k}" = "${EXPECTED_KERNEL}" ]; then
    step_pass "kernel is ${EXPECTED_KERNEL}"
  else
    KERNEL_BAD=1
    step_fail "kernel version" "*** CRITICAL *** expected ${EXPECTED_KERNEL}, got '${k}'. Per ARCHITECTURE.md L-4 / DECISIONS.md D-001, kernel 7.0 causes 'ibv_reg_mr_iova2 failed: Cannot allocate memory' during FlashInfer autotune and kills gx-max. DO NOT start gx-max or gx-reason on this kernel. Reboot into the pinned GRUB entry (6.17.0-1032-nvidia) before doing anything else. See BLOCKERS.md B-001: linux-image-7.0.0-1019-nvidia is still installed on this node and is not apt-marked held."
  fi
}

# --------------------------------------------------------- 4. nvidia/driver --
check_nvidia() {
  local out rc
  out=$(remote nvidia-smi --query-gpu=name,driver_version,temperature.gpu,memory.total,memory.used \
        --format=csv,noheader 2>&1); rc=$?
  if [ "${rc}" -ne 0 ] || [ -z "${out}" ]; then
    step_fail "nvidia-smi" "${out:-<no output>}"
  else
    step_pass "nvidia-smi ok: ${out}"
  fi
}

# --------------------------------------------------------------- 5. docker --
check_docker() {
  local out rc
  out=$(remote docker info --format '{{.ServerVersion}}' 2>&1); rc=$?
  if [ "${rc}" -ne 0 ] || [ -z "${out}" ]; then
    step_fail "docker daemon" "${out:-<no output>}"
    return
  fi
  step_pass "docker daemon reachable (server ${out})"
}

# ------------------------------------------------------------------ 6. rdma --
check_rdma() {
  # Local ICMP check first -- cheap, needs no SSH, and distinguishes "node
  # completely dead" from "node alive but userspace starved" per B-012.
  local a_ok=0 b_ok=0
  ping -c 2 -W 2 "${NODE2_FABRIC_A}" >/dev/null 2>&1 && a_ok=1
  ping -c 2 -W 2 "${NODE2_FABRIC_B}" >/dev/null 2>&1 && b_ok=1
  if [ "${a_ok}" = 1 ] && [ "${b_ok}" = 1 ]; then
    step_pass "ICMP reachable on both fabric rails (${NODE2_FABRIC_A}, ${NODE2_FABRIC_B})"
  else
    step_fail "fabric ICMP" "rail A ok=${a_ok} rail B ok=${b_ok}"
  fi

  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "rdma link state" "ssh not reachable"; return; }
  local out rc
  out=$(remote rdma link show 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "rdma link show" "${out:0:300}"
    return
  fi
  local up_count
  up_count=$(printf '%s\n' "${out}" | grep -c 'ACTIVE')
  if [ "${up_count}" -ge 2 ]; then
    step_pass "rdma link show reports >= 2 ACTIVE ports"
  else
    step_fail "rdma link show" "expected 2 ACTIVE ports (rocep1s0f0, roceP2p1s0f0), found ${up_count}. Full output logged."
  fi
  vlog "rdma link show output:"$'\n'"${out}"
}

# ------------------------------------------------------------------ 7. swap --
check_swap() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "swap" "ssh not reachable"; return; }
  local out rc
  out=$(remote swapon --show=NAME,SIZE --noheadings 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "swap inventory" "${out:0:200}"
    return
  fi
  local line size_raw size_gib
  line=$(printf '%s\n' "${out}" | awk -v n="${SWAPFILE_NAME}" '$1==n{print; exit}')
  if [ -z "${line}" ]; then
    step_fail "swap: ${SWAPFILE_NAME}" "not found in 'swapon --show'. Per ARCHITECTURE.md L-8 this must stay present on both nodes. Full listing: ${out//$'\n'/ | }"
    return
  fi
  size_raw=$(printf '%s\n' "${line}" | awk '{print $2}')
  size_gib=$(printf '%s' "${size_raw}" | tr -dc '0-9.')
  # swapon reports G for GiB-ish sizes here; treat missing unit defensively.
  if awk -v s="${size_gib:-0}" -v m="${SWAPFILE_MIN_GIB}" 'BEGIN{exit !(s+0 >= m+0)}'; then
    step_pass "swap ${SWAPFILE_NAME} present at ${size_raw} (>= ${SWAPFILE_MIN_GIB}G)"
  else
    step_fail "swap: ${SWAPFILE_NAME}" "present but only ${size_raw}, expected >= ${SWAPFILE_MIN_GIB}G"
  fi
}

# ------------------------------------------------------------------ 8. disk --
check_disk() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "disk space" "ssh not reachable"; return; }
  local out rc avail
  out=$(remote df -BG / 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "disk space" "${out:0:200}"
    return
  fi
  avail=$(printf '%s\n' "${out}" | awk 'NR==2{gsub("G","",$4); print $4}')
  if [ -z "${avail}" ]; then
    step_warn "disk space" "could not parse df output: ${out//$'\n'/ | }"
    return
  fi
  if [ "${avail}" -ge "${DISK_MIN_FREE_GB}" ]; then
    step_pass "disk free: ${avail}G on / (>= ${DISK_MIN_FREE_GB}G)"
  else
    step_fail "disk space" "only ${avail}G free on / (< ${DISK_MIN_FREE_GB}G threshold)"
  fi
}

# ---------------------------------------------------- 9/10/11. process state --
STALE_CONTAINERS=()   # names of Exited/Dead containers matched against the allowlist
declare -A CONTAINER_STATUS=()

inspect_processes() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "process inventory" "ssh not reachable"; return; }
  local out rc
  out=$(remote docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_fail "docker ps -a" "${out:0:300}"
    return
  fi
  step_pass "docker ps -a inventory collected ($(printf '%s\n' "${out}" | grep -c . ) container(s))"
  log "container inventory:"
  [ -n "${out}" ] && _write "${out}"

  # Nothing should be running immediately after a fresh boot -- this stack
  # deliberately never auto-starts models (see ARCHITECTURE.md section 8).
  local name status
  while IFS=$'\t' read -r name status _image; do
    [ -z "${name}" ] && continue
    CONTAINER_STATUS["${name}"]="${status}"
    case "${name}" in
      gx-max-rank1)
        if [[ "${status}" == Up* ]]; then
          step_warn "unexpected running container" "gx-max-rank1 is RUNNING but this recovery run did not start it. This must be a leftover from a prior acquire attempt -- do NOT stop it yourself; tell the lead immediately so the orchestrator's state (on node 1) can be reconciled against reality before anyone touches it. (See the lifecycle partial-failure gap noted for Task 9: a failed/timed-out acquire on node 1 does not automatically clean up containers.)"
        fi
        ;;
      gx-reason)
        if [[ "${status}" == Up* ]]; then
          step_warn "unexpected running container" "gx-reason is RUNNING right after recovery. Nothing should auto-start. If this is expected (e.g. llama-swap answered a real request already), fine -- otherwise investigate before proceeding."
        elif [[ "${status}" == Exited* || "${status}" == Dead* ]]; then
          STALE_CONTAINERS+=("gx-reason")
        fi
        ;;
      comfyui|gx-reason-diag-cpu|comfyui-diag)
        if [[ "${status}" == Exited* || "${status}" == Dead* ]]; then
          STALE_CONTAINERS+=("${name}")
        fi
        ;;
    esac
  done <<< "${out}"

  if [ "${#STALE_CONTAINERS[@]}" -gt 0 ]; then
    step_warn "stale containers found" "${STALE_CONTAINERS[*]} (Exited/Dead) -- candidates for cleanup, see step 12"
  else
    step_pass "no stale (Exited/Dead) containers in the known set"
  fi
}

check_lifecycle_lease_staleness() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "lifecycle lease staleness" "ssh not reachable"; return; }
  # The ComfyUI controller (coordination/node2/scripts/gx-node2ctl) tracks its
  # own PID file outside docker. A stale file (process gone) is a classic
  # "lifecycle lease" leftover: it doesn't hurt anything by existing, but a
  # naive health check that only looks at the file would wrongly believe
  # ComfyUI is running.
  local pidfile="/run/user/\$(id -u)/gx-comfyui.pid"
  local out rc
  out=$(remote "f=${pidfile}; if [ -f \"\$f\" ]; then p=\$(cat \"\$f\" 2>/dev/null); if [ -n \"\$p\" ] && kill -0 \"\$p\" 2>/dev/null; then echo \"LIVE \$p\"; else echo \"STALE \$p\"; fi; else echo ABSENT; fi" 2>&1); rc=$?
  if [ "${rc}" -ne 0 ]; then
    step_warn "comfyui pid file check" "could not check: ${out:0:200}"
    return
  fi
  case "${out}" in
    ABSENT) step_pass "no stale comfyui pid file" ;;
    LIVE*)  step_pass "comfyui pid file points at a live process (${out})" ;;
    STALE*) step_warn "stale comfyui pid file" "${out} -- process is gone; candidate for cleanup under --apply" ;;
    *)      step_warn "comfyui pid file check" "unexpected output: ${out}" ;;
  esac

  # ComfyUI runs as the gx-comfyui container behind gx-media-router
  # (~/gx-media, deployed from legenex/media); the router's /health reports it.
  local st; st=$(curl -fsS -m 5 "http://${NODE2_FABRIC_A}:18800/health" 2>&1)
  if [ -n "${st}" ] && [[ "${st}" == *'"status": "ok"'* ]]; then
    vlog "media router health: ${st:0:300}"
  else
    log "media router not answering on ${NODE2_FABRIC_A}:18800 (may simply not be started): ${st:0:150}"
  fi
}

detect_giant_accidental_workload() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "accidental-workload check" "ssh not reachable"; return; }
  local avail rc
  avail=$(remote "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>&1); rc=$?
  if [ "${rc}" -ne 0 ] || [ -z "${avail}" ]; then
    step_warn "memory availability" "could not read MemAvailable: ${avail:-<none>}"
    return
  fi
  log "node2 MemAvailable = ${avail} GiB"
  if [ "${avail}" -lt 60 ]; then
    step_warn "possible accidental workload" "only ${avail} GiB available -- if nothing was deliberately started yet, something large may have auto-started. Re-check the container inventory above before doing anything else. (Normal idle node2 should show ~110+ GiB available.)"
  else
    step_pass "no sign of an accidental large workload (${avail} GiB available)"
  fi
}

# --------------------------------------------- 12. clear stale state (--apply)
clear_stale_state() {
  if [ "${#STALE_CONTAINERS[@]}" -eq 0 ]; then
    step_skip "clear stale state" "nothing to clear"
    return
  fi
  if [ "${APPLY}" -ne 1 ]; then
    step_skip "clear stale state" "--apply not given; would remove: ${STALE_CONTAINERS[*]} (dry run only, nothing changed)"
    return
  fi
  if [ "${KERNEL_BAD}" -eq 1 ]; then
    step_fail "clear stale state" "refusing to modify anything while the kernel check failed -- fix the kernel first"
    return
  fi
  local name safe cleared=()
  for name in "${STALE_CONTAINERS[@]}"; do
    safe=0
    for allowed in "${SAFE_TO_CLEAR_CONTAINERS[@]}"; do
      [ "${name}" = "${allowed}" ] && safe=1 && break
    done
    if [ "${safe}" -ne 1 ]; then
      step_warn "refusing to clear ${name}" "not on the allowlist (${SAFE_TO_CLEAR_CONTAINERS[*]})"
      continue
    fi
    # Belt-and-suspenders: this script never issues a command whose argument
    # mentions /srv/models or /srv/cache; the remote command below only ever
    # names a container, never a path.
    local status="${CONTAINER_STATUS[${name}]:-}"
    if [[ "${status}" == Up* ]]; then
      step_warn "refusing to clear ${name}" "container is RUNNING, not stale -- never removed automatically"
      continue
    fi
    if remote "docker rm -f '${name}'" >/tmp/recover-node2-rm.$$ 2>&1; then
      cleared+=("${name}")
    else
      step_warn "could not clear ${name}" "$(cat /tmp/recover-node2-rm.$$ 2>/dev/null | head -c 200)"
    fi
    rm -f /tmp/recover-node2-rm.$$ 2>/dev/null || true
  done
  if [ "${#cleared[@]}" -gt 0 ]; then
    step_pass "cleared stale container(s): ${cleared[*]}"
  fi
}

# ------------------------------------------------------- 13. service health --
check_node2_services() {
  [ "${NODE2_REACHABLE}" = 1 ] || { step_skip "node2 service health" "ssh not reachable"; return; }

  local out rc
  out=$(remote curl -fsS -m 5 http://127.0.0.1:${SWAP_PORT}/health 2>&1); rc=$?
  if [ "${rc}" -eq 0 ]; then
    step_pass "node2 llama-swap healthy on loopback:${SWAP_PORT}"
  else
    step_warn "node2 llama-swap loopback health" "not answering yet (${out:0:150}) -- may simply not be started; see RECOVERY.md section 4"
  fi

  # This is the address LiteLLM on node 1 actually calls -- the check that
  # matters for whether gx-reason traffic can flow, not just whether the
  # process exists.
  out=$(curl -fsS -m 5 "http://${NODE2_FABRIC_A}:${SWAP_PORT}/health" 2>&1); rc=$?
  if [ "${rc}" -eq 0 ]; then
    step_pass "node2 llama-swap reachable from node1 over the fabric (${NODE2_FABRIC_A}:${SWAP_PORT})"
  else
    step_warn "node2 llama-swap over fabric" "not reachable from node1 yet (${out:0:150})"
  fi
}

# ------------------------------------------------------------------- driver --
check_ssh
check_hostname
check_kernel
check_nvidia
check_docker
check_rdma
check_swap
check_disk
inspect_processes
check_lifecycle_lease_staleness
detect_giant_accidental_workload
clear_stale_state
check_node2_services

log "=============================================================="
log " This script did NOT start gx-max or gx-reason. Those remain OFF"
log " until a real request arrives (on-demand) or a human explicitly"
log " starts them (see legenex/tests/gx-max-validate.sh for gx-max)."
log "=============================================================="
_write ""
_write "=============================================="
printf ' PASS=%d  FAIL=%d  WARN=%d  SKIP=%d\n' "${PASS}" "${FAIL}" "${WARN}" "${SKIP}" | tee -a "${LOG_FILE}"
_write "=============================================="
printf '%s\n' "${RESULTS[@]}" | tee -a "${LOG_FILE}" >/dev/null
printf '%s\n' "${RESULTS[@]}"
_write "full log: ${LOG_FILE}"

exit "${FAIL}"
