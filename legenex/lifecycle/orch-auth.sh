#!/usr/bin/env bash
# Source me. `orch_curl <curl args>` calls the gx-orchestrator with its bearer key (D-044).
# The key comes from GX_ORCHESTRATOR_API_KEY or the protected secrets store, and is passed to
# curl through a config on a pipe, never on the command line (so it is not visible in ps).
_gx_orch_key() {
  local k="${GX_ORCHESTRATOR_API_KEY:-}"
  if [ -z "$k" ] || [ "${k,,}" = not-required ] || [ "${k,,}" = changeme ]; then
    k="$(sed -n 's/^GX_ORCHESTRATOR_API_KEY=//p' "${GX_SECRETS_ENV:-/srv/projects/gx-cluster/secrets/gateway.env}" 2>/dev/null | tail -1)"
    [ "${k,,}" = changeme ] && k=""   # the .env.sample placeholder is not a key
  fi
  printf '%s' "$k"
}
orch_curl() {
  curl -K <(printf 'header = "Authorization: Bearer %s"\n' "$(_gx_orch_key)") "$@"
}
