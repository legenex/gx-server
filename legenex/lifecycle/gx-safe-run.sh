#!/usr/bin/env bash
# ============================================================================
# gx-safe-run.sh — the sanctioned way to start ANY medium/large/exclusive
# container on this node, including one-off diagnostics.
#
# THIS SCRIPT EXISTS BECAUSE OF A REAL INCIDENT (2026-09-14, see
# coordination/BLOCKERS.md B-012): a worker wanted to run a CPU-only
# llama.cpp comparison "just to check something" and started it with a bare
# `docker run`. gx-reason (~95 GiB, mmap'd) was already resident. The second
# ~77 GiB mmap put a 121 GiB node into sustained thrashing -- userspace
# stopped responding on both Tailscale and the fabric, while the kernel
# stayed up (ICMP kept answering). Because mmap pages are reclaimable, the
# OOM killer never stepped in to shed load.
#
# If you are about to start a second llama.cpp/vLLM/SGLang/ComfyUI
# container "just to check something", use this instead of `docker run`
# directly. It costs one extra line and it is the difference between a
# refused launch (this script says no) and a wedged node (a bare `docker
# run` says nothing until it's too late).
#
# Usage:
#   gx-safe-run.sh <node1|node2> <name> <small|medium|large|exclusive> <estimated_gib> -- <command...>
#
# Example -- the exact B-012 scenario, done safely (this call is REFUSED
# outright while gx-reason is resident, exactly as it should have been):
#   gx-safe-run.sh node2 llamacpp-cpu-diag large 77 -- \
#     docker run --rm --name llamacpp-cpu-diag \
#       --device nvidia.com/gpu=all -v /srv/models/gguf:/models/gguf:ro \
#       legenex/llama-cpp-spark:latest -m /models/gguf/... -ngl 0
#
# Exit codes: 0 ran (and registered) | 2 admission refused | 3 node lock
# busy | 64 usage error | anything else = the wrapped command's own exit
# code.
# ============================================================================
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./resource-guard.sh
source "${here}/resource-guard.sh"

if [ "$#" -lt 5 ]; then
  cat >&2 <<'USAGE'
usage: gx-safe-run.sh <node1|node2> <name> <small|medium|large|exclusive> <estimated_gib> -- <command...>

This is the sanctioned way to start a medium/large/exclusive GPU container,
including a one-off diagnostic. See this file's header for why it exists
(coordination/BLOCKERS.md B-012).
USAGE
  exit 64
fi

gx_guard_run "$@"
