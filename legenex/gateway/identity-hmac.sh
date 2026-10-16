#!/usr/bin/env bash
# identity-hmac.sh — provision, verify and roll back the X-GX-Identity signing key (D-054).
#
# The LiteLLM hook signs X-GX-Identity with GX_IDENTITY_HMAC_KEY; the
# orchestrator verifies it with the same key, read from
# /srv/projects/gx-cluster/secrets/orchestrator/identity-hmac on every request.
# A partial rollout (key on one side only) makes every caller anonymous, so
# this script changes both sides together and proves it.
#
#   identity-hmac.sh status      show what is configured (sha256 prefixes only)
#   identity-hmac.sh provision   create the key (0600) if absent, put it in the
#                                ignored gateway .env, recreate ONLY gx-litellm
#   identity-hmac.sh verify      both prefixes equal + one request through
#                                LiteLLM shows a signed identity in the
#                                orchestrator's routing journal
#   identity-hmac.sh rollback    remove the key from both sides (moved aside,
#                                .env backed up), recreate gx-litellm ->
#                                everyone anonymous again
#
# ROTATION = rollback, then provision, then restart the orchestrator:
#   systemctl --user restart gx-orchestrator
# (its unit loads the gateway .env as EnvironmentFile, and an environment key
# wins over the key file, so a running orchestrator keeps the OLD key until it
# restarts; every signed identity is anonymous in between).
#
# No sudo. Never prints a key. Overridable for tests: GX_HMAC_FILE,
# GX_GATEWAY_ENV, GX_COMPOSE_FILE, GX_COMPOSE_PROJECT, GX_LITELLM_CONTAINER,
# GX_DOCKER, GX_GATEWAY_URL, GX_ORCH_URL, GX_SKIP_RECREATE=1, GX_SKIP_REQUEST=1.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HMAC_FILE="${GX_HMAC_FILE:-/srv/projects/gx-cluster/secrets/orchestrator/identity-hmac}"
ENV_FILE="${GX_GATEWAY_ENV:-$HERE/.env}"
COMPOSE_FILE="${GX_COMPOSE_FILE:-$HERE/docker-compose.gateway.yml}"
PROJECT="${GX_COMPOSE_PROJECT:-gx-gateway}"
CONTAINER="${GX_LITELLM_CONTAINER:-gx-litellm}"
DOCKER="${GX_DOCKER:-docker}"
GATEWAY_URL="${GX_GATEWAY_URL:-http://127.0.0.1:4000/v1}"
ORCH_URL="${GX_ORCH_URL:-http://127.0.0.1:18900}"
KEY_NAME=GX_IDENTITY_HMAC_KEY

log() { printf '[identity-hmac] %s\n' "$*"; }
die() { printf '[identity-hmac] ERROR: %s\n' "$*" >&2; exit 1; }
prefix() { sha256sum | cut -c1-16; }

env_value() {   # the value of KEY_NAME in the gateway .env ("" if absent)
    [ -f "$ENV_FILE" ] || { printf ''; return; }
    sed -n "s/^${KEY_NAME}=//p" "$ENV_FILE" | tail -1
}
env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 || true; }

file_prefix() { [ -s "$HMAC_FILE" ] && head -n1 "$HMAC_FILE" | tr -d '\n' | prefix || printf 'absent'; }
env_prefix() { v="$(env_value)"; [ -n "$v" ] && printf '%s' "$v" | prefix || printf 'absent'; }
container_prefix() {
    v="$($DOCKER exec "$CONTAINER" sh -c "printf '%s' \"\${$KEY_NAME:-}\"" 2>/dev/null || true)"
    [ -n "$v" ] && printf '%s' "$v" | prefix || printf 'absent'
}

set_env_line() {   # $1 = value ("" removes the line)
    # The live gateway .env is a SYMLINK into the secrets store (D-043): edit
    # the real target in place, keep temp files and backups beside it (0600,
    # never inside the tree that Open WebUI Computer mounts), and never
    # replace the symlink itself (review of dfefdc3, B2).
    [ -e "$ENV_FILE" ] || die "gateway env file missing: $ENV_FILE"
    local real dir base tmp stamp
    real="$(readlink -f "$ENV_FILE")"
    [ -f "$real" ] || die "gateway env target is not a regular file: $real"
    dir="$(dirname "$real")"; base="$(basename "$real")"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    ( umask 077; cp "$real" "$dir/$base.bak-identity-hmac-$stamp" )
    chmod 600 "$dir/$base.bak-identity-hmac-$stamp"
    # Keep only the newest BACKUPS_KEEP backups so reruns do not pile up.
    ls -1t "$dir/$base".bak-identity-hmac-* 2>/dev/null | tail -n +"$(( ${GX_BACKUPS_KEEP:-3} + 1 ))" \
        | while read -r old; do rm -f -- "$old"; done
    tmp="$(mktemp "$dir/.$base.identity-hmac.XXXXXX")"   # mktemp creates it 0600
    # Every step is checked; on ANY failure the secrets-bearing temp file is
    # removed before we stop, and the original file is left untouched.
    local rc=0
    grep -v "^${KEY_NAME}=" "$real" > "$tmp" || rc=$?
    [ "$rc" -le 1 ] || { rm -f -- "$tmp"; die "could not read $real (grep exit $rc); nothing changed"; }
    if [ -n "$1" ]; then
        printf '%s=%s\n' "$KEY_NAME" "$1" >> "$tmp" || { rm -f -- "$tmp"; die "could not write the temp file; nothing changed"; }
    fi
    chmod --reference="$real" "$tmp" || { rm -f -- "$tmp"; die "could not set the temp file mode; nothing changed"; }
    # same directory: atomic; the symlink keeps pointing at it
    mv -f -- "$tmp" "$real" || { rm -f -- "$tmp"; die "could not replace $real; nothing changed"; }
}

recreate_litellm() {
    if [ "${GX_SKIP_RECREATE:-0}" = 1 ]; then log "GX_SKIP_RECREATE=1: not recreating $CONTAINER"; return; fi
    log "recreating ONLY the LiteLLM service ($CONTAINER); the database and other services are untouched"
    $DOCKER compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --no-deps --force-recreate litellm
    local i
    for i in $(seq 1 $(( ${GX_LIVE_WAIT:-120} / 2 ))); do
        curl -fsS -m 3 "${GATEWAY_URL%/v1}/health/liveliness" >/dev/null 2>&1 && { log "$CONTAINER is live"; return; }
        sleep 2
    done
    die "$CONTAINER did not become live; run: $0 rollback"
}

cmd_status() {
    log "orchestrator key file : $(file_prefix)  ($HMAC_FILE)"
    log "gateway .env          : $(env_prefix)  ($ENV_FILE)"
    log "running $CONTAINER    : $(container_prefix)"
}

cmd_provision() {
    umask 077
    mkdir -p "$(dirname "$HMAC_FILE")"
    chmod 700 "$(dirname "$HMAC_FILE")"
    if [ -s "$HMAC_FILE" ]; then
        log "key file exists; keeping it (rollback first to rotate)"
    else
        python3 -I -c 'import secrets; print(secrets.token_hex(32))' > "$HMAC_FILE"
        log "generated a new key"
    fi
    chmod 600 "$HMAC_FILE"
    set_env_line "$(head -n1 "$HMAC_FILE" | tr -d '\n')"
    recreate_litellm
    cmd_status
    log "next: $0 verify"
}

cmd_verify() {
    local f e c
    f="$(file_prefix)"; e="$(env_prefix)"; c="$(container_prefix)"
    cmd_status
    [ "$f" != absent ] || die "no key file: the orchestrator would treat everyone as anonymous"
    [ "$(stat -c %a "$HMAC_FILE")" = 600 ] || die "$HMAC_FILE must be 0600"
    [ "$f" = "$e" ] || die "the .env key differs from the orchestrator key file"
    [ "$f" = "$c" ] || die "the running $CONTAINER has a different (or no) key: recreate it ($0 provision)"
    log "OK: the same key on both sides ($f)"
    [ "${GX_SKIP_REQUEST:-0}" = 1 ] && return 0
    local gk ok
    gk="${GX_VERIFY_GATEWAY_KEY:-$(env_get LITELLM_MASTER_KEY)}"
    ok="$(env_get GX_ORCHESTRATOR_API_KEY)"
    [ -n "$gk" ] && [ -n "$ok" ] || die "need LITELLM_MASTER_KEY and GX_ORCHESTRATOR_API_KEY in $ENV_FILE"
    GK="$gk" OK="$ok" GW="$GATEWAY_URL" OR="$ORCH_URL" python3 -I - <<'PY'
import hashlib, json, os, sys, time, urllib.error, urllib.request
marker = f"identity-hmac verify {time.time_ns()}"
messages = [{"role": "user", "content": marker}]
body = json.dumps({"model": "gx-auto", "messages": messages, "max_tokens": 1}).encode()
req = urllib.request.Request(os.environ["GW"] + "/chat/completions", data=body, method="POST",
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["GK"],
                                      "X-GX-Project": "identity-hmac-verify", "X-GX-Priority": "background"})
try:
    urllib.request.urlopen(req, timeout=120).read()
except urllib.error.HTTPError as exc:   # a 503 (model down) still writes the decision record
    print(f"[identity-hmac] gateway answered HTTP {exc.code} (fine: the decision is journaled first)")
fp = hashlib.sha256(json.dumps(messages, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:16]
req = urllib.request.Request(os.environ["OR"] + f"/routing/decisions?fingerprint={fp}&limit=5",
                             headers={"Authorization": "Bearer " + os.environ["OK"]})
recs = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("data") or []
dec = [r for r in recs if r.get("event") == "decision"]
if not dec:
    sys.exit("[identity-hmac] FAIL: the orchestrator journaled no decision for the verify request")
who = dec[0].get("identity")
if not who:
    sys.exit("[identity-hmac] FAIL: the decision has no signed identity (anonymous): keys differ or the hook is old")
print(f"[identity-hmac] OK: the orchestrator saw the signed identity {who!r}")
PY
}

cmd_rollback() {
    if [ -s "$HMAC_FILE" ]; then
        mv "$HMAC_FILE" "$HMAC_FILE.disabled-$(date -u +%Y%m%dT%H%M%SZ)"
        ls -1t "$HMAC_FILE".disabled-* 2>/dev/null | tail -n +"$(( ${GX_BACKUPS_KEEP:-3} + 1 ))" \
            | while read -r old; do rm -f -- "$old"; done
        log "orchestrator key moved aside: signed identities stop counting immediately"
    fi
    if [ -n "$(env_value)" ]; then set_env_line ""; log "key removed from $ENV_FILE (backup kept)"; fi
    recreate_litellm
    cmd_status
    log "if gx-orchestrator was started with $KEY_NAME in its environment, restart it: systemctl --user restart gx-orchestrator"
}

case "${1:-status}" in
    status) cmd_status ;;
    provision) cmd_provision ;;
    verify) cmd_verify ;;
    rollback) cmd_rollback ;;
    *) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
