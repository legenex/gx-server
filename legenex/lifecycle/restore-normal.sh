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

# ------------------------------------------------------ drained apps (B-039) --
# ops/dsv41-prestart-drain.sh stops non-model consumers (open-webui,
# gx-computer, and RDP units, runtime-masked so they cannot restart under
# their own steam) to free MemAvailable for the gx-max load transient, and
# records exactly what it touched in RESTORE_MARK. This V4.1 rewrite of
# restore-normal.sh never consumed that marker, so those apps silently
# stayed down after every gx-max release until someone noticed and restarted
# them by hand (evidenced 2026-09-28: open-webui/gx-computer were down for
# hours after a clean gx-max stop.sh release -- see CURRENT_STATE.md D-046
# and coordination/BLOCKERS.md B-039). Consuming the marker here closes that
# gap at its source instead of leaving it to be rediscovered every time.
RESTORE_MARK="${RESTORE_MARK:-/srv/logs/dsv41-restore-after-boot.json}"
if [ -f "${RESTORE_MARK}" ]; then
  log "found drain restore marker ${RESTORE_MARK}; restoring what it stopped"
  mapfile -t _drained_docker < <(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for name in d.get('docker', []):
    print(name)
" "${RESTORE_MARK}" 2>/dev/null)
  for c in "${_drained_docker[@]:-}"; do
    [ -n "${c}" ] || continue
    if docker start "${c}" >/dev/null 2>&1; then
      log "  restarted container ${c}"
    else
      log "  WARN: could not restart container ${c} (docker start failed; check it still exists)"
    fi
  done

  mapfile -t _drained_units < <(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for name in d.get('units', []):
    print(name)
" "${RESTORE_MARK}" 2>/dev/null)
  for u in "${_drained_units[@]:-}"; do
    [ -n "${u}" ] || continue
    # The drain runtime-masks these (systemctl --user mask --runtime) so
    # nothing can start them mid-drain; undo exactly that, and no more --
    # gnome-remote-desktop is socket/session-activated, restoring it to
    # "unmasked" lets it start itself on the next real RDP connection rather
    # than force-starting an interactive desktop service on its behalf.
    if systemctl --user unmask --runtime "${u}" >/dev/null 2>&1; then
      log "  unmasked ${u}"
    else
      log "  WARN: could not unmask ${u}"
    fi
  done

  archived="${RESTORE_MARK}.applied-$(date -u +%Y%m%dT%H%M%SZ)"
  mv -f "${RESTORE_MARK}" "${archived}" 2>/dev/null \
    && log "  archived marker to ${archived}" \
    || log "  WARN: could not archive ${RESTORE_MARK}"
else
  log "no drain restore marker present; nothing to restore beyond the control plane"
fi

log "normal operating state restored"
