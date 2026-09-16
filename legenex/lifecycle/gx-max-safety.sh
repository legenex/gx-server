#!/usr/bin/env bash
# ============================================================================
# gx-max-safety.sh — phase-aware, node-local safety evaluation for gx-max.
#
# Sourced by gx-max-start.sh (node 1, rank0) and rank1-deadman.sh (node 2,
# rank1), so BOTH nodes judge their own health with the same rules and
# neither depends on the other being reachable. Pure bash + /proc, no
# allocation-heavy tooling: it must keep working when the node is short on
# memory.
#
# WHY THIS REPLACED THE 2 GiB MemAvailable TRIPWIRE (2026-09-16)
# ---------------------------------------------------------------
# The verified-working 2026-09-14 launch took node 1 to ~1 GiB MemAvailable
# and 63/63 GB swap while rank0 loaded, then recovered and served. A single
# instantaneous MemAvailable sample below 2 GiB is therefore NORMAL during a
# healthy load: it is the loader's host-side staging spilling into swap. What
# actually distinguishes a failing load from a healthy one is SUSTAINED
# distress, so every rule below is "condition held for N seconds", except the
# ones that are unambiguous on their own (a kernel OOM kill, a driver
# allocation failure).
#
# ABORT RULES (either phase)
#   oom        /proc/vmstat oom_kill increased since arming      -> immediate
#   nvmem      kernel log shows a HARD NV_ERR_NO_MEMORY since arming
#                                                                -> immediate
#              The driver's `nvCheckOkFailedNoLog ... NV_ERR_NO_MEMORY` lines
#              are SOFT: measured 2026-09-16, both nodes emit them at the
#              instant a healthy rank allocates its ~84 GiB parameter store
#              ("Load weight begin"), and the ranks carry on. They are
#              counted (GXS_NV_SOFT) and reported, never fatal on their own.
#              A real allocation failure also kills the rank (rank-death
#              rule in the callers).
#   exhausted  MemAvailable < CRIT_AVAIL_MIB AND SwapFree <
#              CRIT_SWAPFREE_MIB, held EXHAUST_SUSTAIN_S          -> nothing
#              left to absorb the load; the kernel is about to choose
#   thrash     memory PSI full avg10 >= THRASH_PSI_FULL AND swap-in rate >=
#              THRASH_SWAPIN_PPS, held THRASH_SUSTAIN_S           -> pages
#              are being faulted back in as fast as they go out: that is
#              thrashing, not a one-way load spill
#   mgmt       fork+exec of /bin/true slower than FORK_MAX_MS, held
#              MGMT_SUSTAIN_S                                     -> userspace
#              is starving; sshd/tailscaled will be next (B-012/B-020 shape)
# ABORT RULES (steady phase only)
#   steady     MemAvailable < STEADY_FLOOR_GIB, held STEADY_SUSTAIN_S
#
# A one-way swap-out during load (high pswpout, low pswpin) is explicitly NOT
# an abort condition: that is swap doing its job.
#
# Interface:
#   gxs_arm                      snapshot counters; call once before launch
#   gxs_tick <load|steady>       sample + evaluate; sets GXS_VERDICT=ok|abort,
#                                GXS_REASON, and GXS_SAMPLE (one TSV line)
#   gxs_header                   TSV header matching GXS_SAMPLE
# ============================================================================

: "${GXMAX_CRIT_AVAIL_MIB:=512}"
: "${GXMAX_CRIT_SWAPFREE_MIB:=2048}"
: "${GXMAX_EXHAUST_SUSTAIN_S:=30}"
: "${GXMAX_THRASH_PSI_FULL:=40}"
: "${GXMAX_THRASH_SWAPIN_PPS:=4096}"
: "${GXMAX_THRASH_SUSTAIN_S:=120}"
: "${GXMAX_FORK_MAX_MS:=3000}"
: "${GXMAX_MGMT_SUSTAIN_S:=60}"
: "${GXMAX_STEADY_FLOOR_GIB:=4}"
: "${GXMAX_STEADY_SUSTAIN_S:=60}"
# Source files; overridable so the rules can be unit-tested against fakes.
: "${GXS_MEMINFO:=/proc/meminfo}"
: "${GXS_VMSTAT:=/proc/vmstat}"
: "${GXS_PSI:=/proc/pressure/memory}"
: "${GXS_SWAPS:=/proc/swaps}"
# Command that prints kernel log lines since $1; empty disables the probe.
: "${GXS_KLOG_CMD=journalctl -k --no-pager -q --since}"

_gxs_now() { date +%s; }

# Reads the handful of /proc values we need in ONE awk per file.
_gxs_read() {
  read -r GXS_AVAIL_MIB GXS_SWAPFREE_MIB GXS_SWAPTOTAL_MIB < <(
    awk '/^MemAvailable:/{a=$2} /^SwapFree:/{f=$2} /^SwapTotal:/{t=$2}
         END{printf "%d %d %d\n", a/1024, f/1024, t/1024}' "${GXS_MEMINFO:-/proc/meminfo}")
  read -r GXS_PSWPIN GXS_PSWPOUT GXS_OOMKILL < <(
    awk '/^pswpin /{i=$2} /^pswpout /{o=$2} /^oom_kill /{k=$2}
         END{printf "%d %d %d\n", i, o, k}' "${GXS_VMSTAT:-/proc/vmstat}")
  GXS_PSI_FULL10="$(awk '/^full/{split($2,a,"="); printf "%d", a[2]}' "${GXS_PSI:-/proc/pressure/memory}" 2>/dev/null || echo 0)"
  GXS_PSI_SOME10="$(awk '/^some/{split($2,a,"="); printf "%d", a[2]}' "${GXS_PSI:-/proc/pressure/memory}" 2>/dev/null || echo 0)"
}

# Wall time of fork+exec, in ms. Under real starvation this is the first
# thing to degrade and it degrades for every process, sshd included.
_gxs_fork_ms() {
  local t0 t1
  t0=$(date +%s%N)
  /bin/true
  t1=$(date +%s%N)
  echo $(( (t1 - t0) / 1000000 ))
}

gxs_arm() {
  _gxs_read
  GXS_ARMED_AT=$(_gxs_now)
  GXS_OOMKILL0="${GXS_OOMKILL}"
  GXS_LAST_T="${GXS_ARMED_AT}"
  GXS_LAST_PSWPIN="${GXS_PSWPIN}"
  GXS_LAST_PSWPOUT="${GXS_PSWPOUT}"
  GXS_EXHAUST_SINCE=0; GXS_THRASH_SINCE=0; GXS_MGMT_SINCE=0; GXS_STEADY_SINCE=0
  GXS_MIN_AVAIL_MIB="${GXS_AVAIL_MIB}"
  GXS_MAX_SWAPUSED_MIB=$(( GXS_SWAPTOTAL_MIB - GXS_SWAPFREE_MIB ))
  GXS_MAX_PSI_FULL10=0
  GXS_NV_SINCE="$(date -d "@${GXS_ARMED_AT}" '+%Y-%m-%d %H:%M:%S')"
  GXS_NV_HARD=0; GXS_NV_SOFT=0
}

gxs_header() {
  printf 'epoch\tphase\tavail_mib\tswap_used_mib\tsi_pps\tso_pps\tpsi_some10\tpsi_full10\tfork_ms\tverdict\n'
}

# _gxs_hold VAR COND SUSTAIN -> 0 when COND has held for SUSTAIN seconds
_gxs_hold() {
  local var="$1" cond="$2" sustain="$3" now since
  now=$(_gxs_now)
  since="${!var}"
  if [ "${cond}" -eq 1 ]; then
    [ "${since}" -eq 0 ] && { printf -v "${var}" '%s' "${now}"; since="${now}"; }
    [ $(( now - since )) -ge "${sustain}" ] && return 0
  else
    printf -v "${var}" '%s' 0
  fi
  return 1
}

gxs_tick() {
  local phase="${1:-load}" now dt si so fork used c
  _gxs_read
  now=$(_gxs_now)
  dt=$(( now - GXS_LAST_T )); [ "${dt}" -lt 1 ] && dt=1
  si=$(( (GXS_PSWPIN  - GXS_LAST_PSWPIN)  / dt ))
  so=$(( (GXS_PSWPOUT - GXS_LAST_PSWPOUT) / dt ))
  GXS_LAST_T="${now}"; GXS_LAST_PSWPIN="${GXS_PSWPIN}"; GXS_LAST_PSWPOUT="${GXS_PSWPOUT}"
  fork=$(_gxs_fork_ms)
  used=$(( GXS_SWAPTOTAL_MIB - GXS_SWAPFREE_MIB ))

  [ "${GXS_AVAIL_MIB}" -lt "${GXS_MIN_AVAIL_MIB}" ] && GXS_MIN_AVAIL_MIB="${GXS_AVAIL_MIB}"
  [ "${used}" -gt "${GXS_MAX_SWAPUSED_MIB}" ] && GXS_MAX_SWAPUSED_MIB="${used}"
  [ "${GXS_PSI_FULL10}" -gt "${GXS_MAX_PSI_FULL10}" ] && GXS_MAX_PSI_FULL10="${GXS_PSI_FULL10}"

  GXS_VERDICT=ok; GXS_REASON=""

  if [ "${GXS_OOMKILL}" -gt "${GXS_OOMKILL0}" ]; then
    GXS_VERDICT=abort
    GXS_REASON="kernel OOM killer fired ($(( GXS_OOMKILL - GXS_OOMKILL0 )) kill(s) since arming)"
  elif [ -n "${GXS_KLOG_CMD}" ]; then
    local nv
    nv="$(${GXS_KLOG_CMD} "${GXS_NV_SINCE}" 2>/dev/null | awk '/NV_ERR_NO_MEMORY/{ if (/NoLog/) s++; else h++ } END{printf "%d %d", h, s}')"
    GXS_NV_HARD="${nv%% *}"; GXS_NV_SOFT="${nv##* }"
    if [ "${GXS_NV_HARD:-0}" -gt 0 ]; then
      GXS_VERDICT=abort
      GXS_REASON="NVIDIA driver reported a hard NV_ERR_NO_MEMORY (${GXS_NV_HARD} line(s))"
    fi
  fi

  if [ "${GXS_VERDICT}" = ok ]; then
    c=0; { [ "${GXS_AVAIL_MIB}" -lt "${GXMAX_CRIT_AVAIL_MIB}" ] && [ "${GXS_SWAPFREE_MIB}" -lt "${GXMAX_CRIT_SWAPFREE_MIB}" ]; } && c=1
    if _gxs_hold GXS_EXHAUST_SINCE "${c}" "${GXMAX_EXHAUST_SUSTAIN_S}"; then
      GXS_VERDICT=abort
      GXS_REASON="memory AND swap exhausted for >=${GXMAX_EXHAUST_SUSTAIN_S}s (avail ${GXS_AVAIL_MIB}MiB, swap free ${GXS_SWAPFREE_MIB}MiB)"
    fi
  fi
  if [ "${GXS_VERDICT}" = ok ]; then
    c=0; { [ "${GXS_PSI_FULL10}" -ge "${GXMAX_THRASH_PSI_FULL}" ] && [ "${si}" -ge "${GXMAX_THRASH_SWAPIN_PPS}" ]; } && c=1
    if _gxs_hold GXS_THRASH_SINCE "${c}" "${GXMAX_THRASH_SUSTAIN_S}"; then
      GXS_VERDICT=abort
      GXS_REASON="sustained swap thrashing for >=${GXMAX_THRASH_SUSTAIN_S}s (psi full ${GXS_PSI_FULL10}%, swap-in ${si} pages/s)"
    fi
  fi
  if [ "${GXS_VERDICT}" = ok ]; then
    c=0; [ "${fork}" -gt "${GXMAX_FORK_MAX_MS}" ] && c=1
    if _gxs_hold GXS_MGMT_SINCE "${c}" "${GXMAX_MGMT_SUSTAIN_S}"; then
      GXS_VERDICT=abort
      GXS_REASON="management plane degraded: fork+exec ${fork}ms > ${GXMAX_FORK_MAX_MS}ms for >=${GXMAX_MGMT_SUSTAIN_S}s"
    fi
  fi
  if [ "${GXS_VERDICT}" = ok ] && [ "${phase}" = steady ]; then
    c=0; [ $(( GXS_AVAIL_MIB / 1024 )) -lt "${GXMAX_STEADY_FLOOR_GIB}" ] && c=1
    if _gxs_hold GXS_STEADY_SINCE "${c}" "${GXMAX_STEADY_SUSTAIN_S}"; then
      GXS_VERDICT=abort
      GXS_REASON="steady-state MemAvailable ${GXS_AVAIL_MIB}MiB below ${GXMAX_STEADY_FLOOR_GIB}GiB for >=${GXMAX_STEADY_SUSTAIN_S}s"
    fi
  else
    GXS_STEADY_SINCE=0
  fi

  GXS_SAMPLE="$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
    "${now}" "${phase}" "${GXS_AVAIL_MIB}" "${used}" "${si}" "${so}" \
    "${GXS_PSI_SOME10}" "${GXS_PSI_FULL10}" "${fork}" "${GXS_VERDICT}")"
}

# Clean-start probe used by admission on either node. Prints one line of
# key=value facts; the Python policy (resource_guard.takeover-check) decides.
gxs_clean_start_facts() {
  local swapfile="${1:-/swapfile-sglang}" active=0
  _gxs_read
  awk -v f="${swapfile}" 'NR>1 && $1==f{found=1} END{exit !found}' "${GXS_SWAPS:-/proc/swaps}" && active=1
  printf 'avail_mib=%s swap_free_mib=%s swap_total_mib=%s swapfile_active=%s psi_full10=%s oom_kill=%s\n' \
    "${GXS_AVAIL_MIB}" "${GXS_SWAPFREE_MIB}" "${GXS_SWAPTOTAL_MIB}" "${active}" "${GXS_PSI_FULL10}" "${GXS_OOMKILL}"
}
