#!/usr/bin/env bash
# gx-status.sh -- thin wrapper around `python3 -m gx_orchestrator.status_cli`.
#
# The real implementation is a stdlib-only Python module
# (legenex/orchestrator/gx_orchestrator/status_cli.py) so it can reuse the
# SAME per-tier probing logic (health.py / TierHealth) the orchestrator
# itself uses, via its `/health/detailed` endpoint, rather than
# re-implementing probing twice (once in bash, once in Python).
#
# Usage:
#   legenex/scripts/gx-status.sh            # human-readable
#   legenex/scripts/gx-status.sh --json     # machine-readable
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
orchestrator_dir="$(cd "${here}/../orchestrator" && pwd)"

cd "${orchestrator_dir}"
exec python3 -m gx_orchestrator.status_cli "$@" </dev/null
