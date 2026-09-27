#!/usr/bin/env bash
# ============================================================================
# restore-normal.sh — return the cluster to its normal operating state after
# gx-max has released both nodes. (V4.1 rewrite 2026-09-27.)
#
# Normal state:
#   node 1 : LiteLLM gateway (+litellm-db), orchestrator (:18900),
#            Control Center (:8088), Tailscale proxy (:4000)
#   node 2 : nothing resident — the Mia worker rank is launched on demand by
#            mia-dsv41/start.sh over SSH.
#
# The old llama-swap / media / music residents were retired with the
# pre-V4.1 stack (git tag pre-deepseek-v41-rebuild-20260927). This script is
# intentionally idempotent and conservative: it starts the CONTROL plane
# only, never a model.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
gateway="${repo}/legenex/gateway"

log() { printf '[%s] restore: %s\n' "$(date -Is)" "$*" >&2; }
# shellcheck source=./node2-holds.sh
source "${here}/node2-holds.sh"

# ------------------------------------------------------------------- node 1 --
if [ -f "${gateway}/docker-compose.gateway.yml" ] && [ -f "${gateway}/.env" ]; then
  log "starting node-1 gateway stack (litellm + litellm-db)"
  # A caller's environment beats --env-file in compose, and long-running
  # callers (the orchestrator) can hold values older than .env -- a stale
  # placeholder once replaced a real key. Drop every .env name first.
  ( cd "${gateway}" \
    && for v in $(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' .env); do unset "${v}"; done \
    && docker compose --env-file .env -f docker-compose.gateway.yml up -d ) \
    >/dev/null 2>&1 || log "WARN: gateway compose returned non-zero"

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
# gx-auto to work at all. Prefer the systemd user unit; fall back to a detached
# process for non-unit contexts.
if ! curl -fsS -m 3 http://127.0.0.1:18900/health >/dev/null 2>&1; then
  if systemctl --user start gx-orchestrator.service >/dev/null 2>&1; then
    log "started orchestrator via systemd"
  else
    log "starting orchestrator detached"
    ( cd "${repo}/legenex/orchestrator" \
      && setsid python3 -m gx_orchestrator.server >> /srv/logs/gx-orchestrator.log 2>&1 < /dev/null & )
    sleep 3
  fi
fi
curl -fsS -m 3 http://127.0.0.1:18900/health >/dev/null 2>&1 \
  && log "orchestrator is live on :18900" \
  || log "WARN: orchestrator is NOT responding"

# ------------------------------------------------------------------- node 2 --
# Nothing is resident on node 2 in the V4.1 stack; the Mia worker rank is
# launched by mia-dsv41/start.sh on demand and torn down by stop.sh. Only the
# leftover gx-max hold (if any) needs clearing so the resource controller
# does not stay in "held" state.
gx_n2_hold_clear gxmax || log "WARN: could not clear the node-2 gx-max hold"

log "normal operating state restored"
