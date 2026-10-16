#!/usr/bin/env bash
# gateway_config.sh — put the LiteLLM config for a deployment in place, recreate ONLY
# gx-litellm, and prove the identity signing still works.
#
#   legenex/models/dsv4-flash/tools/gateway_config.sh dsv4      # after a V4 Flash switch succeeded
#   legenex/models/dsv4-flash/tools/gateway_config.sh landing   # after switching back to V4.1
#
# dsv4    = legenex/gateway/litellm/config.dsv4-flash.yaml
# landing = legenex/gateway/litellm/config.yaml as landed in 82ca712 (V4.1 model_info)
# Both copy over the live config.yaml (a tracked file: autosync commits it).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
GW="$REPO/legenex/gateway"
LANDING_REF="${GX_LANDING_REF:-82ca712}"
case "${1:-}" in
    dsv4)    cp "$GW/litellm/config.dsv4-flash.yaml" "$GW/litellm/config.yaml" ;;
    landing) git -C "$REPO" show "$LANDING_REF:legenex/gateway/litellm/config.yaml" > "$GW/litellm/config.yaml" ;;
    *) echo "usage: $0 dsv4|landing" >&2; exit 2 ;;
esac
docker compose -p gx-gateway -f "$GW/docker-compose.gateway.yml" --env-file "$GW/.env" \
    up -d --no-deps --force-recreate litellm
for _ in $(seq 1 60); do
    curl -fsS -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null || { echo "gx-litellm is not live" >&2; exit 1; }
"$GW/identity-hmac.sh" verify
echo "gateway config '$1' applied, gx-litellm recreated, identity signing verified"
