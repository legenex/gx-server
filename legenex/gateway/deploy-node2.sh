#!/usr/bin/env bash
# ============================================================================
# deploy-node2.sh -- roll node 2's llama-swap config to the committed code.
#
# Run on gx10-01 (the only Git writer), after autosync has pushed:
#   legenex/gateway/deploy-node2.sh            # deploy and restart llama-swap
#   legenex/gateway/deploy-node2.sh --verify   # check only, change nothing
#
# Why this exists: gx-llama-swap-node02 bind-mounts ~/gx-gateway/node02.yaml,
# NOT the checkout. Editing the checkout therefore does nothing until the file
# is copied across and the container is recreated -- and because the mount pins
# an inode, `docker restart` alone is not always enough after the file is
# replaced, so this recreates the container.
#
# What it does, over the management SSH connection:
#   1. refuses if legenex/gateway has uncommitted changes on gx10-01;
#   2. waits until gx10-02's pull-only checkout is at gx10-01's HEAD;
#   3. refuses while gx-reason is loaded or a gx-max/Maintenance hold exists
#      (recreating llama-swap while it owns a model orphans the container);
#   4. copies llama-swap/node02.yaml into ~/gx-gateway/;
#   5. recreates gx-llama-swap-node02 and health-checks it over the fabric.
# ============================================================================
set -euo pipefail
VERIFY=0
[ "${1:-}" = "--verify" ] && VERIFY=1
N2="${GX_NODE2_SSH:-legenex-02@gx10-02}"
N2_REPO="${GX_NODE2_REPO:-/home/legenex-02/Documents/Projects/Server/gx-cluster}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HEAD="$(git -C "${REPO}" rev-parse HEAD)"

if [ -n "$(git -C "${REPO}" status --porcelain -- legenex/gateway)" ]; then
  echo "legenex/gateway has uncommitted changes on gx10-01; wait for autosync first" >&2
  exit 1
fi

for _ in $(seq 1 60); do
  n2_head="$(ssh -o BatchMode=yes "${N2}" "git -C '${N2_REPO}' rev-parse HEAD")"
  [ "${n2_head}" = "${HEAD}" ] && break
  sleep 5
done
[ "${n2_head}" = "${HEAD}" ] || { echo "gx10-02 is at ${n2_head}, expected ${HEAD}" >&2; exit 1; }

if [ "${VERIFY}" = 1 ]; then
  ssh -o BatchMode=yes "${N2}" \
    "diff -q '${N2_REPO}/legenex/gateway/llama-swap/node02.yaml' ~/gx-gateway/node02.yaml >/dev/null \
     && echo 'node02.yaml: deployed copy matches the checkout' \
     || { echo 'node02.yaml: DEPLOYED COPY IS STALE' >&2; exit 1; }"
  exit $?
fi

ssh -o BatchMode=yes "${N2}" bash -s -- "${N2_REPO}" <<'REMOTE'
set -euo pipefail
repo="$1"
# Never recreate llama-swap while it is serving a model: the running engine
# container would be orphaned and keep its memory.
running="$(docker ps --format '{{.Names}}' | grep -c '^gx-reason$' || true)"
[ "${running}" = 0 ] || { echo "gx-reason is loaded; not recreating llama-swap" >&2; exit 1; }
for hold in node2.maintenance-hold node2.gxmax-hold; do
  [ -e "/srv/projects/gx-cluster/state/guard/${hold}" ] \
    && { echo "${hold} exists; not recreating llama-swap" >&2; exit 1; }
done

cp "${repo}/legenex/gateway/llama-swap/node02.yaml" ~/gx-gateway/node02.yaml
cd ~/gx-gateway
docker compose -f docker-compose.node02.yml up -d --force-recreate llama-swap-node02 2>&1 | tail -2 \
  || docker compose -f docker-compose.node02.yml up -d --force-recreate 2>&1 | tail -2
REMOTE

echo "waiting for node 2 llama-swap to answer on the fabric..."
for _ in $(seq 1 30); do
  if curl -fsS -m 5 -o /dev/null "http://192.168.100.11:28080/health" 2>/dev/null; then
    echo "node 2 llama-swap healthy at ${HEAD:0:12}"
    exit 0
  fi
  sleep 2
done
echo "node 2 llama-swap did not become healthy" >&2
exit 1
