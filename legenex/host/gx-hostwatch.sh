#!/usr/bin/env bash
# ==============================================================================
# gx-hostwatch — local management/host resilience watchdog (gx10-01)
# ==============================================================================
# WHY THIS EXISTS
# ----------------
# On the night of 2026-09-14, gx10-02 was wedged into unresponsiveness by two
# large mmap-backed models resident at once: sustained mmap thrashing left
# userspace (SSH included, over both Tailscale and the ConnectX fabric) unable
# to respond, while the kernel stayed alive (ICMP kept replying). Because mmap
# pages are reclaimable, the OOM killer did not necessarily fire, so the node
# thrashed rather than shedding load. See coordination/BLOCKERS.md B-012.
#
# This script is a passive, read-only early-warning watchdog for THIS node
# (gx10-01). It checks the same class of signal that would have shown gx10-02
# degrading in real time, before SSH became fully unusable:
#   1. sshd  - not just "is the unit active", but "does it still answer the
#      protocol banner on a fresh TCP connection" (a wedged process can still
#      have the kernel ACK new TCP connections from its listen backlog while
#      the userspace accept()/banner-write loop never runs).
#   2. tailscaled - unit state + `tailscale status` backend state.
#   3. general responsiveness - load average vs core count, and a fork/exec
#      latency probe (spawning /bin/true measures real scheduler latency,
#      which degrades under both CPU thrash and severe memory pressure).
#   4. memory pressure - MemAvailable against the mandated floor, plus PSI
#      (/proc/pressure/memory) if the kernel exposes it.
#
# WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
# ------------------------------------------
#   * No remediation. It never kills a container, stops a service, or
#     reboots anything. It only logs and, on a breach, emits a clearly
#     grep-able ALERT line. Automated remediation and the hardware/software
#     watchdog question are separate, deliberately human-gated efforts.
#   * No third-party dependencies. Bash + coreutils + awk, all present on a
#     bare Ubuntu install. `tailscale` and `curl` are used opportunistically
#     (already present on this host) with graceful degradation if missing.
#   * No root. Every check here reads /proc, calls `systemctl is-active`
#     (read-only, works unprivileged), or opens a plain TCP socket.
#
# EVERY external call is wrapped in `timeout` so a wedged target cannot make
# the watchdog itself hang - that would defeat the point.
#
# USAGE
#   ./gx-hostwatch.sh                  # run one check cycle, log, exit
#   GX_HOSTWATCH_MIN_FREE_GIB=40 ./gx-hostwatch.sh   # override a threshold
#
# Installed via systemd --user timer, see legenex/host/systemd/*.{service,timer}
# and the install instructions at the top of gx-hostwatch.service.
# ==============================================================================
set -uo pipefail
# NOTE: deliberately NOT `set -e`. A single failed/degraded check must not
# abort the run before the remaining checks and the summary line are logged.

# ------------------------------------------------------------------ config --
LOG_FILE="${GX_HOSTWATCH_LOG_FILE:-/srv/logs/gx-hostwatch.log}"
MIN_FREE_GIB="${GX_HOSTWATCH_MIN_FREE_GIB:-30}"          # L-task: 30 GiB reserve
LOAD_WARN_RATIO="${GX_HOSTWATCH_LOAD_WARN_RATIO:-1.5}"   # load1 / nproc
LOAD_CRIT_RATIO="${GX_HOSTWATCH_LOAD_CRIT_RATIO:-3.0}"
FORK_WARN_MS="${GX_HOSTWATCH_FORK_WARN_MS:-500}"
FORK_CRIT_MS="${GX_HOSTWATCH_FORK_CRIT_MS:-3000}"
SSH_TIMEOUT_S="${GX_HOSTWATCH_SSH_TIMEOUT_S:-3}"
PSI_FULL_WARN_PCT="${GX_HOSTWATCH_PSI_FULL_WARN_PCT:-5}"   # avg10, %
PSI_FULL_CRIT_PCT="${GX_HOSTWATCH_PSI_FULL_CRIT_PCT:-20}"
SSH_PORT="${GX_HOSTWATCH_SSH_PORT:-22}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

# ---------------------------------------------------------------- logging --
_ts() { date -Is; }

# One structured line per check: ts, level, check name, status, free-form detail.
# Deliberately simple key=value rather than a JSON library - no dependency.
log_line() {
  local level="$1" check="$2" status="$3" detail="$4"
  printf 'ts=%s level=%s check=%s status=%s detail=%q\n' \
    "$(_ts)" "$level" "$check" "$status" "$detail" | tee -a "$LOG_FILE" >&2
}

WARN_COUNT=0
CRIT_COUNT=0
OK_COUNT=0

note() {
  # note LEVEL CHECK STATUS DETAIL
  local level="$1"
  case "$level" in
    OK) OK_COUNT=$((OK_COUNT+1)) ;;
    WARN) WARN_COUNT=$((WARN_COUNT+1)) ;;
    CRIT) CRIT_COUNT=$((CRIT_COUNT+1)) ;;
  esac
  log_line "$level" "$2" "$3" "$4"
}

# ------------------------------------------------------------- 1. sshd --
check_ssh() {
  local unit_state banner start_ns end_ns elapsed_ms
  unit_state=$(timeout 5 systemctl is-active ssh.service 2>&1)
  if [ "$unit_state" != "active" ]; then
    # Fall back to the alternate unit name some distros use.
    unit_state=$(timeout 5 systemctl is-active sshd.service 2>&1)
  fi
  if [ "$unit_state" != "active" ]; then
    note CRIT ssh degraded "unit not active: ${unit_state}"
    return
  fi

  # Banner probe: open a raw TCP socket and read the SSH-2.0-... banner line
  # sshd writes BEFORE authentication. A kernel-alive-but-userspace-wedged
  # sshd can still complete the TCP handshake (kernel accept backlog) while
  # never reaching the point of writing the banner - this is exactly the
  # failure signature from the gx10-02 incident, so this check catches what
  # `systemctl is-active` alone cannot.
  banner=""
  # Braced group (not a subshell) so fd 3 survives for the read below, while
  # still letting us swallow bash's own "connect: Connection refused"
  # diagnostic, which bash prints to stderr immediately on a failed /dev/tcp
  # redirection regardless of trailing `2>` on the `exec` itself.
  if { exec 3<>"/dev/tcp/127.0.0.1/${SSH_PORT}"; } 2>/dev/null; then
    start_ns=$(date +%s%N)
    banner=$(timeout "${SSH_TIMEOUT_S}" head -n1 <&3 2>/dev/null)
    end_ns=$(date +%s%N)
    exec 3<&- 2>/dev/null; exec 3>&- 2>/dev/null
    elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
  else
    note CRIT ssh degraded "could not open TCP connection to 127.0.0.1:${SSH_PORT}"
    return
  fi

  if [ -n "$banner" ]; then
    note OK ssh ok "banner in ${elapsed_ms}ms: ${banner}"
  else
    note CRIT ssh degraded "TCP connected but no SSH banner within ${SSH_TIMEOUT_S}s (unit reports active) - possible mmap/scheduler thrash, see B-012"
  fi
}

# --------------------------------------------------------- 2. tailscaled --
check_tailscale() {
  local unit_state backend
  unit_state=$(timeout 5 systemctl is-active tailscaled.service 2>&1)
  if [ "$unit_state" != "active" ]; then
    note CRIT tailscale degraded "unit not active: ${unit_state}"
    return
  fi
  if ! command -v tailscale >/dev/null 2>&1; then
    note WARN tailscale unknown "unit active but 'tailscale' CLI not present to check backend state"
    return
  fi
  backend=$(timeout 5 tailscale status --json 2>/dev/null | \
    awk -F'"' '/"BackendState"/{print $4; exit}')
  if [ -z "$backend" ]; then
    note CRIT tailscale degraded "unit active but 'tailscale status --json' did not answer within 5s"
  elif [ "$backend" = "Running" ]; then
    note OK tailscale ok "BackendState=Running"
  else
    note WARN tailscale degraded "BackendState=${backend}"
  fi
}

# ------------------------------------------------ 3. general responsiveness --
check_responsiveness() {
  local load1 nproc_n ratio start_ns end_ns elapsed_ms
  load1=$(awk '{print $1}' /proc/loadavg)
  nproc_n=$(nproc 2>/dev/null || echo 1)
  ratio=$(awk -v l="$load1" -v n="$nproc_n" 'BEGIN{ if (n<1) n=1; printf "%.2f", l/n }')

  start_ns=$(date +%s%N)
  timeout 5 /bin/true >/dev/null 2>&1
  end_ns=$(date +%s%N)
  elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))

  local level="OK" detail="load1=${load1} nproc=${nproc_n} load_ratio=${ratio} fork_exec_ms=${elapsed_ms}"
  if awk -v r="$ratio" -v c="$LOAD_CRIT_RATIO" 'BEGIN{exit !(r>c)}' || [ "$elapsed_ms" -ge "$FORK_CRIT_MS" ]; then
    level="CRIT"
  elif awk -v r="$ratio" -v w="$LOAD_WARN_RATIO" 'BEGIN{exit !(r>w)}' || [ "$elapsed_ms" -ge "$FORK_WARN_MS" ]; then
    level="WARN"
  fi
  note "$level" responsiveness "$([ "$level" = OK ] && echo ok || echo degraded)" "$detail"
}

# --------------------------------------------------------- 4. memory --
check_memory() {
  local mem_avail_kib mem_avail_gib detail level="OK" status="ok"
  mem_avail_kib=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  if [ -z "$mem_avail_kib" ]; then
    note WARN memory unknown "MemAvailable not present in /proc/meminfo"
  else
    mem_avail_gib=$(awk -v k="$mem_avail_kib" 'BEGIN{printf "%.2f", k/1048576}')
    detail="MemAvailable=${mem_avail_gib}GiB floor=${MIN_FREE_GIB}GiB"
    if awk -v a="$mem_avail_gib" -v f="$MIN_FREE_GIB" 'BEGIN{exit !(a < f*0.5)}'; then
      level="CRIT"; status="below_half_floor"
    elif awk -v a="$mem_avail_gib" -v f="$MIN_FREE_GIB" 'BEGIN{exit !(a < f)}'; then
      level="WARN"; status="below_floor"
    fi
    note "$level" memory_available "$status" "$detail"
  fi

  # PSI (Pressure Stall Information) - present on this kernel (cgroup v2).
  if [ -r /proc/pressure/memory ]; then
    local some_line full_line some_avg10 full_avg10
    some_line=$(awk '/^some/{print}' /proc/pressure/memory)
    full_line=$(awk '/^full/{print}' /proc/pressure/memory)
    some_avg10=$(printf '%s' "$some_line" | sed -n 's/.*avg10=\([0-9.]*\).*/\1/p')
    full_avg10=$(printf '%s' "$full_line" | sed -n 's/.*avg10=\([0-9.]*\).*/\1/p')
    local plevel="OK" pstatus="ok"
    if awk -v v="${full_avg10:-0}" -v c="$PSI_FULL_CRIT_PCT" 'BEGIN{exit !(v>c)}'; then
      plevel="CRIT"; pstatus="stalled"
    elif awk -v v="${full_avg10:-0}" -v w="$PSI_FULL_WARN_PCT" 'BEGIN{exit !(v>w)}'; then
      plevel="WARN"; pstatus="pressured"
    fi
    note "$plevel" memory_psi "$pstatus" "some_avg10=${some_avg10:-NA} full_avg10=${full_avg10:-NA}"
  else
    note WARN memory_psi unknown "/proc/pressure/memory not readable on this kernel"
  fi
}

# --------------------------------------------------------------- main --
main() {
  log_line INFO run start "gx-hostwatch cycle beginning"
  check_ssh
  check_tailscale
  check_responsiveness
  check_memory

  local overall="ok"
  [ "$WARN_COUNT" -gt 0 ] && overall="degraded"
  [ "$CRIT_COUNT" -gt 0 ] && overall="critical"

  # A single, unambiguous, grep-able line for alerting:
  #   grep 'check=summary' -> then look at status=
  #   grep '^ALERT ' -> only appears when something is not OK.
  log_line INFO summary "$overall" "ok=${OK_COUNT} warn=${WARN_COUNT} crit=${CRIT_COUNT}"
  if [ "$overall" != "ok" ]; then
    printf 'ALERT ts=%s status=%s ok=%s warn=%s crit=%s (see %s)\n' \
      "$(_ts)" "$overall" "$OK_COUNT" "$WARN_COUNT" "$CRIT_COUNT" "$LOG_FILE" | tee -a "$LOG_FILE" >&2
  fi

  # Exit code mirrors severity for anything that later wants to alert on it
  # (e.g. `systemctl --user is-failed` semantics for OnFailure=), but this
  # script itself takes no remediation action either way.
  case "$overall" in
    ok) exit 0 ;;
    degraded) exit 1 ;;
    critical) exit 2 ;;
  esac
}

main "$@"
