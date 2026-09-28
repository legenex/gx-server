#!/usr/bin/env bash
# Pre-start drain for DeepSeek V4.1 / gx-max.
# Frees unified memory on gx10-01 without touching SSH, Tailscale,
# NetworkManager, systemd, docker daemon, or vscode-server.
set -u

MIA="${MIA:-/home/legenex/Documents/Projects/Server/gx-cluster/mia-dsv41}"
WORKER_SSH="${WORKER_SSH:-legenex-02@10.60.21.41}"
TARGET_GIB="${DSV41_START_MEM_GIB:-116}"
FLOOR_GIB="${DSV41_START_MEM_FLOOR_GIB:-112}"
RESTORE_MARK="${RESTORE_MARK:-/srv/state/dsv41-restore-after-boot.json}"
LOG="${DSV41_DRAIN_LOG:-/srv/logs/dsv41-prestart-drain.log}"

need() { awk '/^MemAvailable:/ { printf "%.1f", $2 / 1048576 }' /proc/meminfo; }
ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'; }
log() { printf '[drain] %s %s\n' "$(date -Is)" "$*"; }

mkdir -p /srv/state /srv/logs
exec >>"$LOG" 2>&1

STOPPED_DOCKER=()
STOPPED_UNITS=()
STOPPED_HOST=()
STOPPED_LITELLM=0
MASKED_RDP=0

record() {
  python3 - "$RESTORE_MARK" "${STOPPED_DOCKER[*]-}" "${STOPPED_UNITS[*]-}" "${STOPPED_HOST[*]-}" "$STOPPED_LITELLM" "$MASKED_RDP" <<'PY'
import json, sys, os
path = sys.argv[1]
docker = [x for x in sys.argv[2].split() if x]
units = [x for x in sys.argv[3].split() if x]
host = [x for x in sys.argv[4].split() if x]
payload = {
    "docker": docker,
    "units": units,
    "host": host,
    "litellm": sys.argv[5] == "1",
    "rdp_runtime_masked": sys.argv[6] == "1",
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PY
}

log "begin MemAvailable=$(need) GiB target=${TARGET_GIB} floor=${FLOOR_GIB}"

if [ -x "$MIA/stop.sh" ]; then
  log "stopping stale dsv41 ranks via mia-dsv41/stop.sh"
  bash "$MIA/stop.sh" || log "stop.sh returned non-zero (ok if nothing was running)"
fi

if systemctl --user stop gnome-remote-desktop.service gnome-remote-desktop-handover.service >/dev/null 2>&1; then
  STOPPED_UNITS+=(gnome-remote-desktop.service gnome-remote-desktop-handover.service)
fi
if systemctl --user mask --runtime gnome-remote-desktop.service gnome-remote-desktop-handover.service >/dev/null 2>&1; then
  MASKED_RDP=1
  log "runtime-masked gnome-remote-desktop for this boot"
fi

for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '$2==1000 {print $1}'); do
  T=$(loginctl show-session "$s" -p Type --value 2>/dev/null || true)
  if [ "$T" = "wayland" ] || [ "$T" = "x11" ]; then
    log "terminating graphical session $s type=$T"
    loginctl terminate-session "$s" || true
  fi
done

sleep 2

for c in open-webui gx-computer pageflo-redis-1 pageflo-postgres-1; do
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
    log "docker stop $c"
    docker stop "$c" >/dev/null 2>&1 && STOPPED_DOCKER+=("$c") || true
  fi
done

if pgrep -f '/home/legenex/Documents/Projects/PageFlo/' >/dev/null 2>&1; then
  log "stopping PageFlo host dev processes"
  pkill -f '/home/legenex/Documents/Projects/PageFlo/' || true
  STOPPED_HOST+=(pageflo-dev)
  sleep 1
fi

maybe_stop_unit() {
  local u="$1"
  if systemctl --user is-active --quiet "$u" 2>/dev/null; then
    log "stop user unit $u"
    systemctl --user stop "$u" >/dev/null 2>&1 && STOPPED_UNITS+=("$u") || true
  fi
}

if ! ge "$(need)" "$TARGET_GIB"; then
  maybe_stop_unit nick-wiki-dashboard.service
  maybe_stop_unit legenex-wiki-dashboard.service
  maybe_stop_unit agentos-control-center.service
fi

if ! ge "$(need)" "$FLOOR_GIB"; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gx-litellm; then
    log "MemAvailable=$(need) still below floor — temporarily stopping LiteLLM"
    docker stop gx-litellm >/dev/null 2>&1 && STOPPED_DOCKER+=(gx-litellm) && STOPPED_LITELLM=1 || true
  fi
fi

for i in $(seq 1 45); do
  AV=$(need)
  if ge "$AV" "$TARGET_GIB"; then
    log "target reached MemAvailable=${AV} GiB"
    break
  fi
  sleep 2
done

AV=$(need)
log "done MemAvailable=${AV} GiB docker=${STOPPED_DOCKER[*]-} units=${STOPPED_UNITS[*]-}"
record
if ! ge "$AV" "$FLOOR_GIB"; then
  log "ERROR still below floor ${FLOOR_GIB} GiB"
  exit 1
fi
exit 0
