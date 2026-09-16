#!/usr/bin/env bash
# ============================================================================
# install-sync.sh — install the gx-cluster Git sync role on THIS node.
#
#   ops/git-sync/install-sync.sh writer   # gx10-01 only
#   ops/git-sync/install-sync.sh mirror   # gx10-02 only
#   ops/git-sync/install-sync.sh status
#
# writer: role file, core.hooksPath=.githooks, gx-git-watch (persistent),
#         gx-git-autosync.timer (1 min), gx-git-daily-audit.timer.
# mirror: role file, push URL disabled, no hooks path, gx-git-reconcile.timer
#         (1 min), gx-git-daily-audit.timer. The mirror never gets a GitHub
#         credential; it pulls the public repository over HTTPS.
#
# User systemd units only (no root). Idempotent.
# ============================================================================
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GX_SYNC_TAG="install-sync"
# shellcheck source=./common.sh
source "${here}/common.sh"

ROLE="${1:-status}"
UNIT_DIR="${HOME}/.config/systemd/user"

install_unit() {
  local name="$1"
  mkdir -p "${UNIT_DIR}"
  sed "s#@REPO@#${GX_SYNC_REPO}#g" "${here}/systemd/${name}" > "${UNIT_DIR}/${name}.tmp"
  mv "${UNIT_DIR}/${name}.tmp" "${UNIT_DIR}/${name}"
}

case "${ROLE}" in
  writer)
    case "$(hostname)" in gx10-01*) ;; *) echo "refusing: writer role belongs to gx10-01 (this is $(hostname))" >&2; exit 64 ;; esac
    printf 'writer\n' > "${GX_SYNC_ROLE_FILE}"
    g config core.hooksPath .githooks
    g config push.default current
    g config fetch.prune true
    for u in gx-git-watch.service gx-git-autosync.service gx-git-autosync.timer \
             gx-git-daily-audit.service gx-git-daily-audit.timer; do install_unit "$u"; done
    systemctl --user daemon-reload
    systemctl --user enable --now gx-git-watch.service gx-git-autosync.timer gx-git-daily-audit.timer
    ;;
  mirror)
    case "$(hostname)" in gx10-02*) ;; *) echo "refusing: mirror role belongs to gx10-02 (this is $(hostname))" >&2; exit 64 ;; esac
    printf 'mirror\n' > "${GX_SYNC_ROLE_FILE}"
    g config --unset core.hooksPath 2>/dev/null || true
    g remote set-url origin "${GX_SYNC_PUBLIC_URL}"
    g remote set-url --push origin DISABLED-gx10-02-is-pull-only
    g config fetch.prune true
    for u in gx-git-reconcile.service gx-git-reconcile.timer \
             gx-git-daily-audit.service gx-git-daily-audit.timer; do install_unit "$u"; done
    systemctl --user daemon-reload
    systemctl --user enable --now gx-git-reconcile.timer gx-git-daily-audit.timer
    ;;
  status) ;;
  *) echo "usage: $0 {writer|mirror|status}" >&2; exit 64 ;;
esac

echo "role: $(gxs_role)   repo: ${GX_SYNC_REPO}"
echo "branch: $(g symbolic-ref --quiet --short HEAD 2>/dev/null || echo DETACHED)  HEAD: $(g rev-parse --short HEAD)"
echo "origin fetch: $(g remote get-url origin 2>/dev/null)  push: $(g remote get-url --push origin 2>/dev/null)"
echo "hooksPath: $(g config --get core.hooksPath || echo '(none)')"
systemctl --user list-units --all --no-pager --plain 'gx-git-*' | sed -n '1,12p'
systemctl --user list-timers --all --no-pager 'gx-git-*' | sed -n '1,8p'
