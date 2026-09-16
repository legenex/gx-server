#!/usr/bin/env bash
# Install or repair the control UI on gx10-01. Idempotent; user units only.
#
#   legenex/control-ui/scripts/install.sh           install/refresh the unit and (re)start it
#   legenex/control-ui/scripts/install.sh --check   show what would be installed; change nothing
set -euo pipefail
ui_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "${ui_dir}/../.." && pwd)"
unit_src="${ui_dir}/systemd/gx-control-ui.service"
unit_dst="${HOME}/.config/systemd/user/gx-control-ui.service"
secret_dir="/srv/projects/gx-cluster/secrets/control-ui"
state_dir="/srv/projects/gx-cluster/state/control-ui"
log_dir="/srv/logs/gx-control-ui"

log() { printf '[install] %s\n' "$*"; }

if [ "$(hostname)" != "gx10-01" ]; then
  echo "refusing: the control UI runs on gx10-01 only (this is $(hostname))" >&2
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

mkdir -p "${secret_dir}" "${state_dir}" "${log_dir}" "$(dirname "${unit_dst}")"
chmod 700 "${secret_dir}" "${state_dir}"
chmod 750 "${log_dir}"
chmod +x "${ui_dir}/scripts/"*.sh "${ui_dir}/scripts/gx-ui-passwd"

if [ ! -f "${secret_dir}/auth.json" ]; then
  log "no password configured yet: generating a random initial password"
  "${ui_dir}/scripts/gx-ui-passwd" --generate
fi

if [ ! -f "${unit_dst}" ] || [ "$(cat "${unit_dst}")" != "${rendered}" ]; then
  printf '%s\n' "${rendered}" > "${unit_dst}"
  log "installed ${unit_dst}"
fi
systemctl --user daemon-reload
systemctl --user enable gx-control-ui.service >/dev/null
systemctl --user restart gx-control-ui.service
for _ in $(seq 1 30); do
  if curl -fsS -m 2 http://127.0.0.1:8088/api/health >/dev/null 2>&1; then
    log "gx-control-ui is up: http://127.0.0.1:8088/ and http://$(tailscale ip -4 2>/dev/null | head -n1):8088/"
    curl -sS -m 3 http://127.0.0.1:8088/api/ready; echo
    exit 0
  fi
  sleep 1
done
echo "gx-control-ui did not answer within 30 s; see: journalctl --user -u gx-control-ui -n 50" >&2
exit 1
