#!/usr/bin/env bash
# ============================================================================
# install-node2.sh -- install and start the gx-call supervisor on gx10-02.
#
# Run ON gx10-02 (as legenex-02), from the pull-only checkout:
#   legenex/call/scripts/install-node2.sh
#
# Idempotent. Installs only the LIGHTWEIGHT supervisor, which is safe to start
# at boot: it never loads the VoiceChat engine itself. The engine is started
# on demand when a call session is created, through the node-2 admission guard,
# and stopped after GX_CALL_IDLE_UNLOAD_S idle, in Maintenance, or as soon as
# gx-max claims the node.
#
# What it creates (all outside the checkout, none of it tracked):
#   /srv/projects/gx-cluster/secrets/gx-call/api-key   0600, generated once
#   /srv/projects/gx-cluster/state/gx-call/            session/ledger state
#   /srv/models/voicechat/call-data/                   recordings
#   ~/.config/systemd/user/gx-call.service             symlink to the checkout
# ============================================================================
set -euo pipefail

[ "$(hostname)" = "gx10-02" ] || { echo "run this on gx10-02, not $(hostname)" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
UNIT="${REPO}/legenex/call/systemd/gx-call.service"
[ -f "${UNIT}" ] || { echo "missing ${UNIT}" >&2; exit 1; }

SECRETS=/srv/projects/gx-cluster/secrets/gx-call
STATE=/srv/projects/gx-cluster/state/gx-call
RECORDINGS=/srv/models/voicechat/call-data

mkdir -p "${SECRETS}" "${STATE}" "${RECORDINGS}" /srv/logs/gx-call
chmod 700 "${SECRETS}"

# The supervisor refuses to start without a key of at least 32 characters.
# Generated locally, never printed, never tracked.
if [ ! -s "${SECRETS}/api-key" ]; then
  umask 077
  openssl rand -hex 32 > "${SECRETS}/api-key"
  echo "generated ${SECRETS}/api-key"
else
  echo "reusing existing ${SECRETS}/api-key"
fi
chmod 600 "${SECRETS}/api-key"

mkdir -p ~/.config/systemd/user
ln -sf "${UNIT}" ~/.config/systemd/user/gx-call.service
systemctl --user daemon-reload
systemctl --user enable --now gx-call.service

echo "waiting for the gx-call supervisor to answer..."
for _ in $(seq 1 30); do
  if curl -fsS -m 3 http://127.0.0.1:18840/health >/dev/null 2>&1; then
    echo "gx-call supervisor healthy:"
    curl -fsS -m 3 http://127.0.0.1:18840/health
    echo
    exit 0
  fi
  sleep 1
done

echo "gx-call did not become healthy; last log lines:" >&2
journalctl --user -u gx-call.service --no-pager -n 30 >&2
exit 1
