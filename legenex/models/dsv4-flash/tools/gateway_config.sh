#!/usr/bin/env bash
# gateway_config.sh — put the LiteLLM config for a deployment in place, recreate ONLY
# gx-litellm, and prove the identity signing still works.
#
#   legenex/models/dsv4-flash/tools/gateway_config.sh dsv4      # after a V4 Flash switch succeeded
#   legenex/models/dsv4-flash/tools/gateway_config.sh landing   # after switching back to V4.1
#
# dsv4    = legenex/gateway/litellm/config.dsv4-flash.yaml
# landing = legenex/gateway/litellm/config.dsv41.yaml (V4.1 model_info; tracked, so a
#           rollback restores the CURRENT V4.1 values, e.g. gx-fast's 64K window)
# Both copy over the live config.yaml (a tracked file: autosync commits it).
set -euo pipefail
REPO="${GX_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
GW="$REPO/legenex/gateway"
GATEWAY_URL="${GX_GATEWAY_URL:-http://127.0.0.1:4000/v1}"
DOCKER="${GX_DOCKER:-docker}"
CONTAINER="${GX_LITELLM_CONTAINER:-gx-litellm}"
target="${1:-}"
other=landing; [ "$target" = landing ] && other=dsv4
trap 'rc=$?; echo "[gateway_config] FAILED (exit $rc). Way back: $0 $other   (gateway config);  $GW/identity-hmac.sh rollback   (identity signing, only if verify keeps failing)" >&2' ERR

# Refuse to recreate the LIVE gx-litellm from any checkout other than the one
# it was created from (a worktree would swap in its own mounts).
want="$(realpath "$GW/docker-compose.gateway.yml")"
have="$($DOCKER inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$CONTAINER" 2>/dev/null || true)"
if [ -z "$have" ] || [ "$(realpath -m "$have")" != "$want" ]; then
    trap - ERR
    echo "[gateway_config] REFUSED: $CONTAINER was created from '${have:-unknown}', not $want; run this script from that checkout" >&2
    exit 3
fi
case "$target" in
    dsv4)    cp "$GW/litellm/config.dsv4-flash.yaml" "$GW/litellm/config.yaml" ;;
    landing) cp "$GW/litellm/config.dsv41.yaml" "$GW/litellm/config.yaml" ;;
    *) echo "usage: $0 dsv4|landing" >&2; exit 2 ;;
esac
$DOCKER compose -p gx-gateway -f "$GW/docker-compose.gateway.yml" --env-file "$GW/.env" \
    up -d --no-deps --force-recreate litellm
for _ in $(seq 1 $(( ${GX_LIVE_WAIT:-120} / 2 ))); do
    curl -fsS -m 3 "${GATEWAY_URL%/v1}/health/liveliness" >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS -m 3 "${GATEWAY_URL%/v1}/health/liveliness" >/dev/null || { echo "gx-litellm is not live" >&2; false; }
"$GW/identity-hmac.sh" verify
trap - ERR
echo "gateway config '$target' applied, gx-litellm recreated, identity signing verified"
