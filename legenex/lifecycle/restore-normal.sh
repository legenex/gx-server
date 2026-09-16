#!/usr/bin/env bash
# ============================================================================
# restore-normal.sh — return the cluster to its normal operating state after
# gx-max has released both nodes.
#
# Normal state:
#   node 1 : LiteLLM gateway + llama-swap (gx-mini / gx-fast on demand)
#   node 2 : llama-swap (gx-reason on demand), media services on demand
#
# This is intentionally idempotent and conservative: it starts the CONTROL
# plane, not the models. llama-swap loads models on demand, so nothing large is
# allocated until a request actually arrives. That satisfies the requirement
# that model processes must not all start at once and exhaust memory.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
gateway="${repo}/legenex/gateway"

log() { printf '[%s] restore: %s\n' "$(date -Is)" "$*" >&2; }

# ------------------------------------------------------------------- node 1 --
if [ -f "${gateway}/docker-compose.gateway.yml" ] && [ -f "${gateway}/.env" ]; then
  log "starting node-1 gateway stack"
  ( cd "${gateway}" && docker compose --env-file .env -f docker-compose.gateway.yml up -d ) \
    >/dev/null 2>&1 || log "WARN: gateway compose returned non-zero"

  # Wait for the gateway to answer rather than assuming it did.
  for _ in $(seq 1 60); do
    if curl -fsS -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
      log "LiteLLM gateway is live on :4000"
      break
    fi
    sleep 2
  done
else
  log "WARN: gateway compose or .env missing at ${gateway}; skipping node 1"
fi

# The orchestrator owns gx-max acquisition, so it must be running for gx-max and
# gx-auto to work at all. Start it if nothing is listening.
if ! curl -fsS -m 3 http://127.0.0.1:18900/health >/dev/null 2>&1; then
  log "starting orchestrator"
  ( cd "${repo}/legenex/orchestrator" \
    && setsid python3 -m gx_orchestrator.server >> /srv/logs/gx-orchestrator.log 2>&1 < /dev/null & ) \
    || log "WARN: could not start orchestrator"
  sleep 3
fi
curl -fsS -m 3 http://127.0.0.1:18900/health >/dev/null 2>&1 \
  && log "orchestrator is live on :18900" \
  || log "WARN: orchestrator is NOT responding"

# ------------------------------------------------------------------- node 2 --
# Node 2's llama-swap is managed by the worker; start it if its compose exists.
if ssh -o BatchMode=yes -o ConnectTimeout=10 legenex-02@gx10-02 \
     'test -f ~/gx-gateway/docker-compose.node02.yml' 2>/dev/null; then
  log "starting node-2 llama-swap"
  ssh -o BatchMode=yes legenex-02@gx10-02 \
    'cd ~/gx-gateway && docker compose -f docker-compose.node02.yml up -d' >/dev/null 2>&1 \
    || log "WARN: node-2 compose returned non-zero"
else
  log "node-2 gateway compose not present yet; skipping"
fi

# ------------------------------------------------------------ node 2 media --
# gx-max-start.sh's drain stops gx-comfyui AND gx-media-router (they are in
# CONFLICTS_N2), but nothing used to start them again -- so every gx-max
# attempt, successful or refused, silently left gx-image and gx-video dead
# until someone noticed. Found 2026-09-16 when an acceptance run reported
# "media router/ComfyUI not reachable" immediately after a gx-max test.
#
# Same principle as the rest of this script: start the CONTROL plane, not the
# models. An idle ComfyUI is ~0.7 GiB (measured); it loads weights only when a
# generation request actually arrives.
if ssh -o BatchMode=yes -o ConnectTimeout=10 legenex-02@gx10-02 \
     'test -f ~/gx-media/docker-compose.media.yml' 2>/dev/null; then
  log "starting node-2 media stack"
  ssh -o BatchMode=yes -o ConnectTimeout=15 legenex-02@gx10-02 \
    'cd ~/gx-media && docker compose -f docker-compose.media.yml up -d' >/dev/null 2>&1 \
    || log "WARN: node-2 media compose returned non-zero"
  for _ in $(seq 1 30); do
    if curl -fsS -m 3 http://192.168.100.11:18800/health >/dev/null 2>&1; then
      log "media router is live on 192.168.100.11:18800"
      break
    fi
    sleep 2
  done
else
  log "node-2 media compose not present; skipping"
fi

log "normal operating state restored"
