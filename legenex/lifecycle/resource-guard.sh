#!/usr/bin/env bash
# ============================================================================
# resource-guard.sh — THE sanctioned admission-control primitive for the
# gx-cluster lifecycle scripts.
#
# Born from the 2026-09-14 incident (coordination/BLOCKERS.md B-012): a
# CPU-only diagnostic was started with a bare `docker run` while gx-reason
# (~95 GiB, mmap'd) was still resident on a 121 GiB node. That bypassed
# llama-swap's own model-group exclusivity entirely, because `docker run`
# does not go through llama-swap at all. mmap pages are reclaimable, so the
# OOM killer never fired -- the node thrashed indefinitely instead
# (userspace unresponsive, kernel/ICMP still alive).
#
# This file, and gx-safe-run.sh next to it, exist so that:
#   * the admission ARITHMETIC lives in exactly one place --
#     legenex/orchestrator/gx_orchestrator/resource_guard.py -- and neither
#     this file nor any caller re-derives it in shell;
#   * the LOCK is a real kernel primitive (flock(2) on a plain file) at a
#     path both this file and the Python orchestrator's NodeLock agree on,
#     so a bash-invoked launch and a Python-invoked launch for the same node
#     are genuinely mutually exclusive, not just "usually fine";
#   * nothing can silently downgrade a refusal into a warning: `gx_guard_run`
#     returns a non-zero exit code and NEVER executes the wrapped command
#     when admission is refused.
#
# Nothing stops a person or an agent from still typing `docker run` by hand.
# What this buys is a documented, one-line-longer, structurally obvious
# alternative: every lifecycle script in this directory (gx-max-start.sh
# included) routes its large/exclusive launches through `gx_guard_run`, and
# any one-off diagnostic should use `gx-safe-run.sh` instead of `docker run`
# directly -- see that file's header for the exact incident it would have
# prevented.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/resource-guard.sh"
#   gx_guard_run node2 gx-reason large 95 -- \
#     docker run -d --name gx-reason ... "${IMAGE}" ...
#
# Exit codes from gx_guard_run: 0 = ran (and registered) | 2 = admission
# refused | 3 = node lock busy (another launch in progress) | 64 = usage
# error | anything else = the wrapped command's own exit code.
# ============================================================================
set -uo pipefail

_guard_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# All state (lock files + residency ledgers) lives here. Override for tests.
GX_GUARD_STATE_DIR="${GX_GUARD_STATE_DIR:-${_guard_here}/.state}"

# Human-mandated floor, 2026-09-14: never budget a node down to the wire.
GX_GUARD_RESERVE_GIB="${GX_GUARD_RESERVE_GIB:-30}"

# 128 GB advertised, ~121 GiB actually usable (measured MemTotal).
GX_GUARD_NODE_TOTAL_GIB="${GX_GUARD_NODE_TOTAL_GIB:-121}"

# How long a launch attempt waits for a busy node lock before giving up.
GX_GUARD_LOCK_TIMEOUT="${GX_GUARD_LOCK_TIMEOUT:-30}"

GX_GUARD_PY="${GX_GUARD_PY:-python3}"
GX_GUARD_ORCH_DIR="${GX_GUARD_ORCH_DIR:-${_guard_here}/../orchestrator}"
GX_GUARD_MEMINFO="${GX_GUARD_MEMINFO:-/proc/meminfo}"

mkdir -p "${GX_GUARD_STATE_DIR}"

_guard_log() { printf '[%s] guard: %s\n' "$(date -Is)" "$*" >&2; }

# ---------------------------------------------------------------- plumbing --
# gx_guard_check NODE NAME CLASS ESTIMATED_GIB
# Pure admission decision (no locking of its own -- the caller must already
# hold the node lock, e.g. via gx_guard_run or its own `flock`). Prints the
# admission JSON on stdout. Returns 0 = allowed, 2 = refused.
gx_guard_check() {
  local node="$1" name="$2" class="$3" est="$4"
  ( cd "${GX_GUARD_ORCH_DIR}" && "${GX_GUARD_PY}" -m gx_orchestrator.resource_guard \
      --state-dir "${GX_GUARD_STATE_DIR}" check \
      --node "${node}" --name "${name}" --class "${class}" --estimated-gib "${est}" \
      --reserve-gib "${GX_GUARD_RESERVE_GIB}" --node-total-gib "${GX_GUARD_NODE_TOTAL_GIB}" \
      --meminfo-path "${GX_GUARD_MEMINFO}" )
}

# gx_guard_register NODE NAME CLASS ESTIMATED_GIB [CONTAINER]
gx_guard_register() {
  local node="$1" name="$2" class="$3" est="$4" container="${5:-$2}"
  ( cd "${GX_GUARD_ORCH_DIR}" && "${GX_GUARD_PY}" -m gx_orchestrator.resource_guard \
      --state-dir "${GX_GUARD_STATE_DIR}" register \
      --node "${node}" --name "${name}" --class "${class}" --estimated-gib "${est}" --container "${container}" )
}

# gx_guard_release NODE NAME
gx_guard_release() {
  local node="$1" name="$2"
  ( cd "${GX_GUARD_ORCH_DIR}" && "${GX_GUARD_PY}" -m gx_orchestrator.resource_guard \
      --state-dir "${GX_GUARD_STATE_DIR}" release --node "${node}" --name "${name}" )
}

gx_guard_status() {
  local node="$1"
  ( cd "${GX_GUARD_ORCH_DIR}" && "${GX_GUARD_PY}" -m gx_orchestrator.resource_guard \
      --state-dir "${GX_GUARD_STATE_DIR}" status --node "${node}" )
}

# ------------------------------------------------------------- the gate --
# gx_guard_run NODE NAME CLASS ESTIMATED_GIB -- CMD...
#
# THE sanctioned launch path: acquire the node's flock -> run the shared
# admission check -> run the caller's command -> register residency on
# success, all inside ONE critical section. The lock is released the moment
# this function returns (success OR failure), never left dangling: a
# `docker run -d` that returns quickly hands the node back to other callers
# right away, while a second concurrent gx_guard_run/gx-max-start.sh for the
# same node blocks (up to GX_GUARD_LOCK_TIMEOUT) rather than racing past the
# admission check.
gx_guard_run() {
  local node="$1" name="$2" class="$3" est="$4"; shift 4
  if [ "${1:-}" != "--" ]; then
    _guard_log "FATAL: gx_guard_run missing '--' before the command"
    return 64
  fi
  shift

  local lockfile="${GX_GUARD_STATE_DIR}/${node}.lock"
  local guard_fd
  exec {guard_fd}>"${lockfile}"
  if ! flock -x -w "${GX_GUARD_LOCK_TIMEOUT}" "${guard_fd}"; then
    _guard_log "REFUSED: node ${node} lock busy after ${GX_GUARD_LOCK_TIMEOUT}s (another launch in progress) -- name=${name}"
    eval "exec ${guard_fd}>&-"
    return 3
  fi
  _guard_log "lock acquired: ${lockfile} (name=${name} class=${class} estimated_gib=${est})"

  local admission rc
  if ! admission="$(gx_guard_check "${node}" "${name}" "${class}" "${est}")"; then
    _guard_log "REFUSED admission for ${name} on ${node}: ${admission}"
    eval "exec ${guard_fd}>&-"
    return 2
  fi
  _guard_log "admitted: ${admission}"

  _guard_log "launching (name=${name}): $*"
  "$@"
  rc=$?

  if [ "${rc}" -eq 0 ]; then
    gx_guard_register "${node}" "${name}" "${class}" "${est}" "${name}"
    _guard_log "registered ${name} as resident on ${node} (${est} GiB, class=${class})"
  else
    _guard_log "launch command for ${name} failed (exit ${rc}); nothing registered"
  fi

  eval "exec ${guard_fd}>&-"
  return "${rc}"
}
