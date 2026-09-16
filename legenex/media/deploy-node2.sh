#!/usr/bin/env bash
# ============================================================================
# deploy-node2.sh -- roll the media ROUTER on gx10-02 to the committed code.
#
# Run on gx10-01 (the only Git writer), after autosync has pushed:
#   legenex/media/deploy-node2.sh
#
# What it does, over the management SSH connection:
#   1. waits until gx10-02's pull-only checkout is at gx10-01's HEAD;
#   2. copies legenex/media from that checkout into ~/gx-media (the live
#      compose directory), keeping ~/gx-media/.env (the media key);
#   3. rebuilds gx-media-router and recreates ONLY that container
#      (ComfyUI is not restarted; its loaded models stay);
#   4. checks /health over the fabric.
# It never starts, stops or rebuilds ComfyUI, and never touches a model.
# ============================================================================
set -euo pipefail
N2="${GX_NODE2_SSH:-legenex-02@gx10-02}"
N2_REPO="${GX_NODE2_REPO:-/home/legenex-02/Documents/Projects/Server/gx-cluster}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HEAD="$(git -C "${REPO}" rev-parse HEAD)"

if [ -n "$(git -C "${REPO}" status --porcelain -- legenex/media)" ]; then
  echo "legenex/media has uncommitted changes on gx10-01; wait for autosync first" >&2
  exit 1
fi
for _ in $(seq 1 60); do
  n2_head="$(ssh -o BatchMode=yes "${N2}" "git -C '${N2_REPO}' rev-parse HEAD")"
  [ "${n2_head}" = "${HEAD}" ] && break
  sleep 5
done
[ "${n2_head}" = "${HEAD}" ] || { echo "gx10-02 is at ${n2_head}, expected ${HEAD}" >&2; exit 1; }

ssh -o BatchMode=yes "${N2}" bash -s -- "${N2_REPO}" <<'REMOTE'
set -euo pipefail
src="$1/legenex/media"
cd ~/gx-media
rsync -a --exclude .env --exclude __pycache__ --exclude tests/ "${src}/" ~/gx-media/
docker compose -f docker-compose.media.yml build router >/dev/null
docker compose -f docker-compose.media.yml up -d --no-deps router 2>&1 | tail -1
for _ in $(seq 1 30); do
  if curl -fsS -m 3 http://192.168.100.11:18800/health >/dev/null 2>&1; then
    curl -fsS http://192.168.100.11:18800/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print("router", d["status"], "uploads", d.get("uploads_enabled"), "workflows", len(d["workflows"]))'
    exit 0
  fi
  sleep 2
done
echo "router did not become healthy" >&2
exit 1
REMOTE
echo "deployed media router at ${HEAD:0:12}"
