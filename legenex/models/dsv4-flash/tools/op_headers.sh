#!/usr/bin/env bash
# op_headers.sh — write the orchestrator operator headers to a 0600 file and print its path.
#
#   HF=$(legenex/models/dsv4-flash/tools/op_headers.sh <your-name>)
#   curl -s -H @"$HF" http://127.0.0.1:18900/models/status
#   ...
#   rm -f "$HF"          # or: op_headers.sh --cleanup
#
# Keys never reach argv, the process list, the screen or shell history: they
# are read from their files and written with the printf builtin into a file
# created under umask 077 in $XDG_RUNTIME_DIR (tmpfs, per user, 0700).
set -euo pipefail
if [ "${1:-}" = --cleanup ]; then
    rm -f -- "${XDG_RUNTIME_DIR:?}/gx-op.headers"
    echo "removed ${XDG_RUNTIME_DIR}/gx-op.headers" >&2
    exit 0
fi
name="${1:-operator}"
name="$(printf '%s' "$name" | tr -cd 'A-Za-z0-9._@ -' | cut -c1-48)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
ENV_FILE="${GX_GATEWAY_ENV:-$REPO/legenex/gateway/.env}"
TOKEN_FILE="${GX_OPERATOR_TOKEN_FILE:-/srv/projects/gx-cluster/secrets/orchestrator/operator-token}"
DIR="${XDG_RUNTIME_DIR:-}"
[ -n "$DIR" ] && [ -d "$DIR" ] || { echo "XDG_RUNTIME_DIR is not set; refusing to put keys elsewhere" >&2; exit 2; }
[ -s "$TOKEN_FILE" ] || { echo "no operator token at $TOKEN_FILE (step W0)" >&2; exit 2; }
okey="$(sed -n 's/^GX_ORCHESTRATOR_API_KEY=//p' "$ENV_FILE" | tail -1)"
[ -n "$okey" ] || { echo "GX_ORCHESTRATOR_API_KEY missing in $ENV_FILE" >&2; exit 2; }
umask 077
out="$DIR/gx-op.headers"
{
    printf 'Authorization: Bearer %s\n' "$okey"
    printf 'X-GX-Operator-Token: %s\n' "$(head -n1 "$TOKEN_FILE" | tr -d '\r\n')"
    printf 'X-GX-Operator: %s\n' "$name"
    printf 'Content-Type: application/json\n'
} > "$out"
chmod 600 "$out"
printf '%s\n' "$out"
echo "headers in $out (0600). In this shell: trap 'rm -f \"\$HF\"' EXIT   -- or later: $0 --cleanup" >&2
