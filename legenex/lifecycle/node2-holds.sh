#!/usr/bin/env bash
# ============================================================================
# node2-holds.sh — node-2 hold files and the gx-music drain (D-036).
#
# Sourced by gx-max-start.sh, gx-max-stop.sh, gx-max-unwind.sh and
# restore-normal.sh. Everything here runs ON gx10-02 through one SSH call per
# step; nothing is started, only held, drained, verified and released.
#
# The gx-max hold (state/guard/node2.gxmax-hold on node 2) is the explicit
# "gx-max owns this node" signal. gx-music's supervisor (legenex/music)
# refuses to load while it is fresh and unloads a loaded engine within one
# reaper tick (15 s). The drain below therefore:
#
#   1. writes the hold and proves it exists;
#   2. waits for the SUPERVISOR to unload the engine (queued music jobs stay
#      queued with the reason "gx-max is starting on the cluster");
#   3. only if that does not happen in time, stops the engine container
#      itself (never the supervisor);
#   4. verifies: no gx-music container, no gx-music ledger entry, no
#      ACE-Step process, and reports MemAvailable.
#
# Return codes: 0 verified | 1 verification failed | 5 node 2 unreachable.
# ============================================================================

GX_N2_GUARD_DIR="${GX_N2_GUARD_DIR:-/srv/projects/gx-cluster/state/guard}"
GX_N2_REPO="${GX_N2_REPO:-/home/legenex-02/Documents/Projects/Server/gx-cluster}"
GX_MUSIC_CONTAINER="${GX_MUSIC_CONTAINER:-gx-music}"
GX_MUSIC_DRAIN_TIMEOUT="${GX_MUSIC_DRAIN_TIMEOUT:-120}"
GX_MUSIC_PORT="${GX_MUSIC_PORT:-18820}"
GX_MUSIC_FABRIC_HOST="${GX_MUSIC_FABRIC_HOST:-192.168.100.11}"

_gxh_log() { printf '[%s] node2-holds: %s\n' "$(date -Is)" "$*" >&2; }

# Remote shell on node 2. Tests override GXH_N2_CMD with a local runner.
_gxh_n2() {
  if [ -n "${GXH_N2_CMD:-}" ]; then
    ${GXH_N2_CMD} "$@"
  else
    ssh -o BatchMode=yes -o ConnectTimeout=10 "${GXMAX_NODE2_SSH:-legenex-02@gx10-02}" "$@"
  fi
}

# gx_n2_hold_set NAME   (NAME = gxmax | maintenance)
gx_n2_hold_set() {
  local name="$1" f
  f="${GX_N2_GUARD_DIR}/node2.${name}-hold"
  if _gxh_n2 "mkdir -p '${GX_N2_GUARD_DIR}' && date -Is > '${f}' && test -f '${f}'" >/dev/null 2>&1; then
    _gxh_log "hold set on node2: ${f}"
    return 0
  fi
  _gxh_log "FAILED to set hold ${f} on node2"
  return 5
}

# gx_n2_hold_clear NAME -- removes the hold and proves it is gone.
gx_n2_hold_clear() {
  local name="$1" f
  f="${GX_N2_GUARD_DIR}/node2.${name}-hold"
  if _gxh_n2 "rm -f '${f}' && test ! -e '${f}'" >/dev/null 2>&1; then
    _gxh_log "hold cleared on node2: ${f}"
    return 0
  fi
  _gxh_log "FAILED to clear hold ${f} on node2"
  return 1
}

# gx_music_drain_node2 -- steps 2-4 above. Prints one summary line on stdout.
gx_music_drain_node2() {
  local out rc
  out="$(_gxh_n2 "bash -s" <<EOF
set -u
c='${GX_MUSIC_CONTAINER}'; g='${GX_N2_GUARD_DIR}'; t=${GX_MUSIC_DRAIN_TIMEOUT}
start=\$(date +%s); how=none
if docker inspect "\$c" >/dev/null 2>&1; then
  how=supervisor
  while docker inspect "\$c" >/dev/null 2>&1; do
    if [ \$(( \$(date +%s) - start )) -ge "\$t" ]; then
      how=fallback-stop
      docker stop -t 30 "\$c" >/dev/null 2>&1
      docker rm -f "\$c" >/dev/null 2>&1
      break
    fi
    sleep 3
  done
fi
waited=\$(( \$(date +%s) - start ))
# The supervisor releases its ledger entry itself; release again if it could not.
if [ -f "\$g/node2-residency.json" ] && grep -q '"gx-music"' "\$g/node2-residency.json"; then
  if [ -d '${GX_N2_REPO}/legenex/orchestrator' ]; then
    ( cd '${GX_N2_REPO}/legenex/orchestrator' && python3 -m gx_orchestrator.resource_guard \
        --state-dir "\$g" release --node node2 --name gx-music ) >/dev/null 2>&1
  fi
fi
container=absent; docker inspect "\$c" >/dev/null 2>&1 && container=present
ledger=clean
[ -f "\$g/node2-residency.json" ] && grep -q '"gx-music"' "\$g/node2-residency.json" && ledger=dirty
procs=\$(pgrep -fc 'acestep[.]api_server' 2>/dev/null || true); procs=\${procs:-0}
avail=\$(awk '/^MemAvailable:/{printf "%d", \$2/1048576}' /proc/meminfo)
echo "music_drain how=\$how waited_s=\$waited container=\$container ledger=\$ledger engine_procs=\$procs mem_available_gib=\$avail"
[ "\$container" = absent ] && [ "\$ledger" = clean ] && [ "\$procs" = 0 ]
EOF
)"; rc=$?
  printf '%s\n' "${out}" | tail -1
  if [ "${rc}" -eq 255 ] || [ -z "${out}" ]; then
    _gxh_log "node2 unreachable during the music drain"
    return 5
  fi
  [ "${rc}" -eq 0 ] && return 0
  _gxh_log "music drain NOT verified: ${out}"
  return 1
}

# gx_music_supervisor_ensure -- after a release: the light supervisor must be
# running (it never loads weights on its own). Prints the health line.
gx_music_supervisor_ensure() {
  local out
  out="$(_gxh_n2 "systemctl --user cat gx-music.service >/dev/null 2>&1 || { echo 'health=no-unit'; exit 0; }; \
    systemctl --user is-active --quiet gx-music.service || systemctl --user start gx-music.service; \
    for i in 1 2 3 4 5 6 7 8 9 10; do \
      h=\$(curl -fsS -m 3 http://${GX_MUSIC_FABRIC_HOST}:${GX_MUSIC_PORT}/health 2>/dev/null) && { echo \"health=ok \$h\"; exit 0; }; sleep 2; done; \
    echo 'health=down'; exit 1" 2>/dev/null)"
  local rc=$?
  printf '%s\n' "${out}" | tail -1
  return "${rc}"
}
