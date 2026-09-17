#!/usr/bin/env bash
# QA gate for gx-voice. Hermetic: no GPU, no Docker daemon, no model.
#   ./qa.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 byte-compile =="
python3 -m compileall -q gx_voice scripts tests engine/gx_voice_engine.py

echo "== 2/5 shell syntax =="
for f in scripts/*.sh qa.sh; do bash -n "$f" && echo "  ok  $f"; done

echo "== 3/5 lint (ruff, if the Control Center venv has it) =="
ruff=../control-ui/.venv/bin/ruff
if [ -x "$ruff" ]; then
  "$ruff" check --config ../control-ui/pyproject.toml gx_voice tests scripts engine/gx_voice_engine.py
else
  echo "  skipped: $ruff not found"
fi

echo "== 4/5 unit + protocol integration tests =="
if ! out="$(python3 -m unittest discover -s tests -t . 2>&1)"; then
  printf '%s\n' "$out" | tail -40; echo "  FAIL unit tests"; exit 1
fi
printf '%s\n' "$out" | tail -3

echo "== 5/5 no literal credentials in the tree =="
if grep -rInE '(api[_-]?key|password|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' \
     --include='*.py' --include='*.sh' --include='*.service' --include='*.md' --include='Dockerfile' . \
     | grep -v 'KEY = "k" \* 40'; then
  echo "  FAIL literal credential found"; exit 1
fi
echo "  ok  none"
echo "QA PASSED"
