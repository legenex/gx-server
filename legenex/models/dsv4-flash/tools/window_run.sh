#!/usr/bin/env bash
# Recovered W1-W5 entry point; Python owns the durable activation state machine.
set -euo pipefail
exec /usr/bin/python3 -I "$(dirname "${BASH_SOURCE[0]}")/window_runner.py" "$@"
