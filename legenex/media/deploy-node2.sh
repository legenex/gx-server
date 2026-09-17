#!/usr/bin/env bash
# ============================================================================
# deploy-node2.sh -- roll the media ROUTER on gx10-02 to the committed code.
#
# Run on gx10-01 (the only Git writer), after autosync has pushed:
#   legenex/media/deploy-node2.sh                 # router only
#   legenex/media/deploy-node2.sh --with-comfyui  # also recreate ComfyUI when its
#                                                 # compose settings changed (drops
#                                                 # loaded models; refused while busy)
#
# What it does, over the management SSH connection:
#   1. waits until gx10-02's pull-only checkout is at gx10-01's HEAD;
#   2. copies legenex/media from that checkout into ~/gx-media (the live
#      compose directory), keeping ~/gx-media/.env (the media key);
#   3. rebuilds gx-media-router and recreates ONLY that container
#      (ComfyUI is not restarted; its loaded models stay);
#   4. checks /health over the fabric.
# Without --with-comfyui it never restarts ComfyUI, and it never touches a model.
# ============================================================================
set -euo pipefail
WITH_COMFY=0
[ "${1:-}" = "--with-comfyui" ] && WITH_COMFY=1
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

if [ "${WITH_COMFY}" = 1 ]; then
  busy="$(curl -fsS -m 5 http://192.168.100.11:18800/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(bool(d.get("busy") or d.get("video_queue_depth"))))')"
  [ "${busy}" = 0 ] || { echo "a media generation is running; not recreating ComfyUI" >&2; exit 1; }
  gx_state="$(curl -fsS -m 5 http://127.0.0.1:18900/lifecycle/gx-max/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"
  [ "${gx_state}" = down ] || { echo "gx-max is ${gx_state}; not touching the media stack" >&2; exit 1; }
fi

ssh -o BatchMode=yes "${N2}" bash -s -- "${N2_REPO}" "${WITH_COMFY}" <<'REMOTE'
set -euo pipefail
src="$1/legenex/media"
with_comfy="$2"
cd ~/gx-media
rsync -a --exclude .env --exclude __pycache__ --exclude tests/ "${src}/" ~/gx-media/
docker compose -f docker-compose.media.yml build router >/dev/null
docker compose -f docker-compose.media.yml up -d --no-deps router 2>&1 | tail -1
if [ "${with_comfy}" = 1 ]; then
  docker compose -f docker-compose.media.yml up -d --no-deps --no-build comfyui 2>&1 | tail -1
  for _ in $(seq 1 150); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' gx-comfyui 2>/dev/null)" = healthy ] && break
    sleep 2
  done
  echo "comfyui args: $(docker inspect -f '{{join .Args " "}}' gx-comfyui | grep -o -- '--reserve-vram [0-9.]*')"
fi
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
