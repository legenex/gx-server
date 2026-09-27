#!/usr/bin/env bash
# Shared helpers for gx-max lifecycle scripts (V4.1: the Mia kit).
#
# The SGLang argument-vector builders (gxmax_args / gxmax_env_flags /
# gxmax_docker_flags) are RETIRED with the old engine: the Mia kit
# (mia-dsv41/start.sh) owns the launch, driven entirely by environment
# variables. These helpers now cover only the cluster-level facts:
# logging, node-2 access, the serving endpoint, and the profile overlay.
#
# NOTE: this library deliberately does NOT set -e. It is sourced by both
# fail-fast scripts (start/stop, which set their own -euo pipefail) and by
# tolerant ones (status, which must keep reporting when a probe fails).

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_here}/gx-max.conf"

log() { printf '[%s] %s\n' "$(date -Is)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

n2() { ssh -o BatchMode=yes -o ConnectTimeout=10 "${GXMAX_NODE2_SSH}" "$@"; }

# ------------------------------------------------------------- the endpoint -
# /health answers before the model is loadable; readiness means health AND
# /v1/models advertising the RIGHT served id (a proxy answering without the
# model is a config fault, not a healthy engine).
gxmax_healthy() {
  curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/health" >/dev/null 2>&1
}

gxmax_serves_model() {
  curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/v1/models" 2>/dev/null \
    | grep -q "\"id\"[[:space:]]*:[[:space:]]*\"${GXMAX_SERVED_MODEL_ID}\""
}

gxmax_ready() { gxmax_healthy && gxmax_serves_model; }

# In-flight request count from vLLM's Prometheus endpoint. Returns a number,
# or "unknown" when /metrics is not reachable (the graceful drain then falls
# back to a fixed quiet period instead of pretending the engine is idle).
gxmax_inflight() {
  local body
  body=$(curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/metrics" 2>/dev/null) || { echo unknown; return; }
  printf '%s\n' "$body" | awk '
    /^vllm:num_requests_running/{r+=$2}
    /^vllm:num_requests_waiting/{q+=$2}
    END{ printf "%d", r+q }'
}

# ------------------------------------------------------------ containers ---
gxmax_rank0_running() { [ "$(docker inspect -f '{{.State.Running}}' "${GXMAX_RANK0_NAME}" 2>/dev/null || echo false)" = "true" ]; }
gxmax_rank1_running() { [ "$(n2 "docker inspect -f '{{.State.Running}}' ${GXMAX_RANK1_NAME} 2>/dev/null || echo false")" = "true" ]; }

# --------------------------------------------------------- profile overlay -
# The registry (legenex/models/registry.json) drives the engine through
# environment variables; the orchestrator (gx_orchestrator.lifecycle) passes
# the real per-profile values. This renders the overlay for a log line and a
# sanity check -- the kit's own defaults apply for anything left unset.
gxmax_profile_env() {
  printf 'GXMAX_PROFILE=%s\n' "${GXMAX_PROFILE}"
  printf 'SERVED_MODEL_NAME=%s\n' "${SERVED_MODEL_NAME}"
  printf 'MAX_NUM_SEQS=%s\n' "${MAX_NUM_SEQS}"
  printf 'SPEC_METHOD=%s\n' "${SPEC_METHOD}"
  printf 'DSPARK_TOKENS=%s\n' "${DSPARK_TOKENS:-}"
  printf 'MAX_MODEL_LEN=%s\n' "${MAX_MODEL_LEN}"
}
