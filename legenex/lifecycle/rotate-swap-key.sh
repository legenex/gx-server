#!/usr/bin/env bash
# Rotate GX_SWAP_API_KEY (the llama-swap bearer) on BOTH nodes (D-044, B-033).
#
#   rotate-swap-key.sh --preflight   read-only: prove the cutover is safe right now
#   rotate-swap-key.sh --execute     do it (make-before-break where the software allows; rolls back on failure)
#
# Why this is an outage: llama-swap reads the key from its container environment only, and the
# resident text models run inside its network namespace. Recreating llama-swap therefore unloads
# and preloads gx-mini and gx-code again. The key cannot be rolled node by node: LiteLLM holds a
# single key for both backends, so once node 2 has the new key LiteLLM is rejected there until it
# is recreated too. Expected: gx-mini back in about 1 minute, gx-code (a backend on each node,
# reloading in parallel) in about 4 minutes; gx-auto follows gx-code. About 5 minutes in all.
#
# Consumers of the key: llama-swap node 1 (gateway/.env), llama-swap node 2 (~/gx-gateway/.env on
# gx10-02), gx-litellm (gateway/.env), gx-orchestrator and gx-control-ui (gateway/.env).
# Nothing else holds it (media router, voice, music, call, live were checked).
#
# Secrets never appear on a command line or in output.
set -euo pipefail
umask 077

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS_ENV="${GX_SECRETS_ENV:-/srv/projects/gx-cluster/secrets/gateway.env}"
BACKUPS="${GX_BACKUPS:-/srv/projects/gx-cluster/backups}"
N2="${GX_NODE2_SSH:-legenex-02@gx10-02}"
N2_GW="${GX_NODE2_GATEWAY_DIR:-~/gx-gateway}"
gateway="${repo}/legenex/gateway"
. "${repo}/legenex/lifecycle/orch-auth.sh"

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { log "ABORT: $*"; exit 1; }
getvar() { sed -n "s/^$1=//p" "$SECRETS_ENV" | tail -1; }

preflight() {
  local bad=0
  ssh -o ConnectTimeout=8 -o BatchMode=yes "$N2" true 2>/dev/null || { log "FAIL gx10-02 unreachable over ssh"; bad=1; }
  [ -r "$SECRETS_ENV" ] && [ "$(stat -c %a "$SECRETS_ENV")" = 600 ] || { log "FAIL $SECRETS_ENV missing or not 0600"; bad=1; }
  local st; st="$(orch_curl -fsS -m 8 http://127.0.0.1:18900/lifecycle/gx-max/status 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)"
  [ "$st" = down ] && log "ok   gx-max is down" || { log "FAIL gx-max state is '${st:-unknown}' (must be down)"; bad=1; }
  local busy media_up
  media_up="$(ssh "$N2" 'docker ps --format "{{.Names}}" | grep -Ec "comfyui|media-router|^router$" || true' 2>/dev/null)"
  if [ "${media_up:-0}" = 0 ]; then
    log "ok   media stack is not running on node 2"
  else
    busy="$(curl -fsS -m 6 http://192.168.100.11:18800/health 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(int(bool(d.get("busy") or d.get("video_queue_depth"))))' 2>/dev/null || echo x)"
    [ "$busy" = 0 ] && log "ok   no media generation running" || { log "FAIL media stack up but busy or unreachable (${busy})"; bad=1; }
  fi
  local in_flight; in_flight="$(docker exec gx-litellm sh -c 'ss -tn state established "( dport = :19001 or dport = :19011 or dport = :8080 )" 2>/dev/null | wc -l' 2>/dev/null || echo 0)"
  log "info established upstream connections from gx-litellm: ${in_flight}"
  local a1; a1="$(free -g | awk '/^Mem:/{print $7}')"
  local a2; a2="$(ssh "$N2" "free -g | awk '/^Mem:/{print \$7}'" 2>/dev/null || echo 0)"
  [ "${a1:-0}" -ge 30 ] && [ "${a2:-0}" -ge 30 ] && log "ok   available RAM node1=${a1}G node2=${a2}G" || { log "FAIL available RAM node1=${a1}G node2=${a2}G (need >=30G each to reload gx-code)"; bad=1; }
  local old; old="$(getvar GX_SWAP_API_KEY)"
  local h1 h2
  h1="$(bearer_curl "$old" -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:28080/running)"
  h2="$(ssh "$N2" 'K=$(sed -n "s/^GX_SWAP_API_KEY=//p" '"$N2_GW"'/.env); printf "header = \"Authorization: Bearer %s\"\n" "$K" | curl -s -m5 -o /dev/null -w "%{http_code}" -K /dev/stdin http://192.168.100.11:28080/running' 2>/dev/null || echo x)"
  [ "$h1" = 200 ] && [ "$h2" = 200 ] && log "ok   current key accepted by both llama-swaps" || { log "FAIL current key rejected (node1=$h1 node2=$h2)"; bad=1; }
  [ "$(cd "$repo" && git status --porcelain | wc -l)" = 0 ] && log "ok   repository tree clean" || log "info repository tree has pending changes"
  return $bad
}

rollback() {
  log "ROLLBACK: restoring the previous key on both nodes"
  cp -p "${BK}/gateway.env" "$SECRETS_ENV"
  ssh "$N2" "cp -p ${N2_GW}/.env.pre-rotate ${N2_GW}/.env" || true
  recreate_all || true
}

recreate_node2() { ssh "$N2" "cd ${N2_GW} && docker compose -f docker-compose.node02.yml up -d --force-recreate llama-swap-node02" >/dev/null; }
recreate_node1() {
  ( cd "$gateway"
    for v in $(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' .env); do unset "$v"; done
    docker compose --env-file .env -f docker-compose.gateway.yml up -d --force-recreate --no-deps llama-swap-node01
    docker compose --env-file .env -f docker-compose.gateway.yml up -d --force-recreate --no-deps litellm ) >/dev/null
}
recreate_all() { recreate_node2; recreate_node1; systemctl --user restart gx-orchestrator.service gx-control-ui.service; }

bearer_curl() {    # bearer_curl <key> <curl args...> — key via a curl config on a pipe, never argv
  local k="$1"; shift
  curl -K <(printf 'header = "Authorization: Bearer %s"\n' "$k") "$@"
}

wait_running() {   # wait_running <name> <url> <deadline-seconds> ; needs $KEY
  local t=$((SECONDS + $3))
  until bearer_curl "$KEY" -fsS -m5 "$2/running" 2>/dev/null | grep -q '"state":"ready"'; do
    [ $SECONDS -lt $t ] || return 1; sleep 10
  done
}

case "${1:-}" in
  --preflight) preflight && { log "SWAP ROTATION preflight: all checks passed"; } ;;
  --execute)
    preflight || die "preflight failed; nothing was changed"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"; BK="${BACKUPS}/swap-rotation-${stamp}"; mkdir -p -m 700 "$BK"
    cp -p "$SECRETS_ENV" "${BK}/gateway.env"
    ssh "$N2" "cp -p ${N2_GW}/.env ${N2_GW}/.env.pre-rotate"
    NEW="sk-swap-$(openssl rand -hex 32)"
    python3 - "$SECRETS_ENV" "$NEW" <<'PY'
import os, re, sys
p, new = sys.argv[1:]
s = re.sub(r'(?m)^GX_SWAP_API_KEY=.*$', 'GX_SWAP_API_KEY=' + new, open(p).read())
open(p + '.tmp', 'w').write(s); os.chmod(p + '.tmp', 0o600); os.replace(p + '.tmp', p)
PY
    printf '%s' "$NEW" | ssh "$N2" "K=\$(cat); python3 - \"\$K\" <<'PY'
import os, re, sys, pathlib
p = pathlib.Path(os.path.expanduser('${N2_GW}/.env')); s = p.read_text()
s = re.sub(r'(?m)^GX_SWAP_API_KEY=.*\$', 'GX_SWAP_API_KEY=' + sys.argv[1], s)
t = p.with_name('.env.tmp'); t.write_text(s); os.chmod(t, 0o600); os.replace(t, p)
PY"
    unset NEW
    KEY="$(getvar GX_SWAP_API_KEY)"
    trap 'rollback' ERR
    log "node 2: recreate llama-swap (gx-code reloads)"
    recreate_node2
    log "node 1: recreate llama-swap and gx-litellm (gx-mini reloads in about 10 s, gx-code in about 3 min)"
    recreate_node1
    systemctl --user restart gx-orchestrator.service gx-control-ui.service
    log "waiting for the resident models"
    wait_running node1 http://127.0.0.1:28080 600 || die "node 1 models did not come back"
    wait_running node2 http://192.168.100.11:28080 600 || die "node 2 models did not come back"
    old_rejected="$(bearer_curl "$(sed -n 's/^GX_SWAP_API_KEY=//p' "${BK}/gateway.env")" -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:28080/running)"
    [ "$old_rejected" = 401 ] || die "old key still accepted on node 1 (HTTP ${old_rejected})"
    trap - ERR
    log "SWAP ROTATION complete; previous key rejected; backup at ${BK} (remove it once verified)"
    ;;
  *) echo "usage: $0 --preflight | --execute" >&2; exit 2 ;;
esac
