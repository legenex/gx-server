#!/usr/bin/env bash
# Install or repair GX-Playground on gx10-01. Idempotent; user units only.
#   legenex/playground/scripts/install.sh          install/refresh the unit and (re)start it
#   legenex/playground/scripts/install.sh --check  show what would change; change nothing
set -euo pipefail
pg_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "${pg_dir}/../.." && pwd)"
unit_src="${pg_dir}/systemd/gx-playground.service"
unit_dst="${HOME}/.config/systemd/user/gx-playground.service"
log_dir="/srv/logs/gx-playground"

log() { printf '[install] %s\n' "$*"; }

if [ "$(hostname)" != "gx10-01" ]; then
  echo "refusing: GX-Playground runs on gx10-01 only (this is $(hostname))" >&2
  exit 1
fi
rendered="$(sed "s#@REPO@#${repo}#g" "${unit_src}")"
if [ "${1:-}" = "--check" ]; then
  if [ -f "${unit_dst}" ] && [ "$(cat "${unit_dst}")" = "${rendered}" ]; then
    log "unit up to date: ${unit_dst}"
  else
    log "unit would be (re)installed: ${unit_dst}"
  fi
  exit 0
fi
mkdir -p "${log_dir}" "$(dirname "${unit_dst}")"
chmod 750 "${log_dir}"
if [ ! -f "${unit_dst}" ] || [ "$(cat "${unit_dst}")" != "${rendered}" ]; then
  printf '%s\n' "${rendered}" > "${unit_dst}"
  log "installed ${unit_dst}"
fi
systemctl --user daemon-reload
systemctl --user enable gx-playground.service >/dev/null
systemctl --user restart gx-playground.service
for _ in $(seq 1 30); do
  if curl -fsS -m 2 http://127.0.0.1:8090/pg/health >/dev/null 2>&1; then
    log "gx-playground is up: http://127.0.0.1:8090/ and http://$(tailscale ip -4 2>/dev/null | head -n1):8090/"
    curl -sS -m 3 http://127.0.0.1:8090/pg/health; echo
    exit 0
  fi
  sleep 1
done
log "gx-playground did not answer on :8090 within 30 s"
systemctl --user status gx-playground.service --no-pager | tail -20
exit 1
