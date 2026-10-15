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
#   5. GX boot headroom - while the DeepSeek model is NOT loaded, whether this
#      head node could boot it at all: MemAvailable against mia-dsv41's own
#      requirement (weight budget per rank + DSV41_BOOT_MARGIN_GIB). GX was down
#      2026-10-14..15 because a resident desktop session left ~107 GiB against
#      111.5 GiB needed, while check 4 (30 GiB floor) said OK. WARN names the
#      largest anonymous-memory consumers; a margin below the kit default is
#      reported, never changed.
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
SWAPFILE_NAME="${GX_HOSTWATCH_SWAPFILE_NAME:-/swapfile-sglang}"
SWAPFILE_MIN_GIB="${GX_HOSTWATCH_SWAPFILE_MIN_GIB:-47}"   # L-8: keep 48G, recover-node2.sh uses the same >=47 tolerance
DSV41_DIR="${GX_HOSTWATCH_DSV41_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)/mia-dsv41}"
DSV41_HEAD_CONTAINER="${GX_HOSTWATCH_DSV41_HEAD:-dsv41-exl3-head}"
DSV41_KIT_MARGIN_GIB=12   # mia-dsv41 start.sh default (headless boot)

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

# ------------------------------------------------------------ 5. swapfile --
# L-8 requires /swapfile-sglang present and active on both nodes; the gx-max
# admission guard (resource_guard.py) hard-refuses without it, but that is
# only checked at the moment someone tries to acquire gx-max - it can go
# missing (accidental or deliberate `swapoff`+`rm`) and sit undetected for
# hours. This check catches it within one cycle (<=60s) instead. Read-only:
# `swapon --show` and /proc/swaps both work unprivileged.
check_swap() {
  local line size_raw
  line=$(timeout 5 swapon --show=NAME,SIZE --noheadings 2>/dev/null | \
    awk -v n="$SWAPFILE_NAME" '$1==n{print; exit}')
  if [ -z "$line" ]; then
    note CRIT swapfile missing "${SWAPFILE_NAME} not in 'swapon --show' - L-8 violated, gx-max admission will hard-refuse until 'sudo fallocate -l 48G ${SWAPFILE_NAME} && sudo chmod 600 ${SWAPFILE_NAME} && sudo mkswap ${SWAPFILE_NAME} && sudo swapon ${SWAPFILE_NAME}' is re-run (needs root)"
    return
  fi
  size_raw=$(printf '%s\n' "$line" | awk '{print $2}')
  if [[ "$size_raw" =~ ^([0-9.]+)G$ ]] && \
     awk -v g="${BASH_REMATCH[1]}" -v m="$SWAPFILE_MIN_GIB" 'BEGIN{exit !(g>=m)}'; then
    note OK swapfile ok "${SWAPFILE_NAME} present at ${size_raw} (>= ${SWAPFILE_MIN_GIB}G)"
  else
    note CRIT swapfile undersized "${SWAPFILE_NAME} present but reports ${size_raw}, expected >= ${SWAPFILE_MIN_GIB}G"
  fi
}

# ------------------------------------------------- 5. GX boot headroom --
_dsv41_env() {  # _dsv41_env KEY DEFAULT: value from mia-dsv41/.env (last assignment), else DEFAULT
  local v
  v=$(sed -n "s/^$1=//p" "$DSV41_DIR/.env" 2>/dev/null | tail -1 | tr -d '"'"'"'')
  printf '%s' "${v:-$2}"
}

_top_anon() {  # the three user cgroups holding the most anonymous memory, "name=GiB"
  local base="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service"
  [ -d "$base" ] || return 0
  find "$base" -mindepth 2 -maxdepth 3 -name memory.stat 2>/dev/null | while read -r f; do
    awk -v n="$(basename "$(dirname "$f")")" '/^anon /{printf "%d %s\n", $2, n}' "$f"
  done | sort -rn | head -3 | awk '{printf "%s%s=%.1fGiB", (NR>1?",":""), $2, $1/1073741824}'
}

check_boot_headroom() {
  if [ ! -x "$DSV41_DIR/start.sh" ]; then
    note OK gx_boot_headroom not_applicable "no mia-dsv41 at $DSV41_DIR"
    return
  fi
  local names
  if ! names=$(timeout 10 docker ps --format '{{.Names}}' 2>/dev/null); then
    # Under load (e.g. mid model boot) docker can be slow: never call that "unloaded".
    note WARN gx_boot_headroom unknown "docker ps failed or timed out; cannot tell whether $DSV41_HEAD_CONTAINER is up"
    return
  fi
  if printf '%s\n' "$names" | grep -qx "$DSV41_HEAD_CONTAINER"; then
    note OK gx_boot_headroom model_resident "$DSV41_HEAD_CONTAINER running; boot headroom not applicable"
    return
  fi
  local model tp margin per_rank need avail detail level="OK" status="ok"
  model=$(_dsv41_env MODEL_HOST "")
  tp=$(_dsv41_env TP 2)
  margin=$(_dsv41_env DSV41_BOOT_MARGIN_GIB "$DSV41_KIT_MARGIN_GIB")
  per_rank=$(timeout 30 python3 "$DSV41_DIR/scripts/weight_budget.py" --model "$model" --tp "$tp" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["per_rank_bytes"])' 2>/dev/null)
  if ! [[ "${per_rank:-}" =~ ^[0-9]+$ ]]; then
    note WARN gx_boot_headroom unknown "weight budget unavailable for MODEL_HOST=${model:-unset}"
    return
  fi
  need=$(awk -v b="$per_rank" -v m="$margin" 'BEGIN{printf "%.1f", b/1073741824 + m}')
  avail=$(awk '/^MemAvailable:/{printf "%.1f", $2/1048576}' /proc/meminfo)
  detail="MemAvailable=${avail}GiB need=${need}GiB (weights $(awk -v b="$per_rank" 'BEGIN{printf "%.1f", b/1073741824}') + margin ${margin})"
  if awk -v a="$avail" -v n="$need" 'BEGIN{exit !(a < n)}'; then
    level="WARN"; status="insufficient"
    detail="$detail; gx-auto/gx-max cannot load until $(awk -v a="$avail" -v n="$need" 'BEGIN{printf "%.1f", n-a}')GiB is freed; top anon: $(_top_anon)"
  fi
  note "$level" gx_boot_headroom "$status" "$detail"
  if awk -v m="$margin" -v k="$DSV41_KIT_MARGIN_GIB" 'BEGIN{exit !(m < k)}'; then
    note WARN gx_boot_margin lowered "DSV41_BOOT_MARGIN_GIB=${margin} in $DSV41_DIR/.env is below the kit default ${DSV41_KIT_MARGIN_GIB}"
  fi
}

# --------------------------------------------------------------- main --
main() {
  log_line INFO run start "gx-hostwatch cycle beginning"
  check_ssh
  check_tailscale
  check_responsiveness
  check_memory
  check_swap
  check_boot_headroom

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
