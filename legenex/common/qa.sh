#!/usr/bin/env bash
# QA for legenex/common (gxcommon): lint + unit tests. Stdlib only.
set -euo pipefail
cd "$(dirname "$0")"
ui=../control-ui
echo "== lint =="
"${ui}/.venv/bin/ruff" check --config "${ui}/pyproject.toml" gxcommon tests
"${ui}/.venv/bin/mypy" --config-file "${ui}/pyproject.toml" gxcommon
python3 -m compileall -q gxcommon
echo "== unit tests =="
python3 -m unittest discover -s tests 2>&1 | tail -3
echo "QA PASSED"
