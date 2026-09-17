#!/usr/bin/env bash
# QA gate for gx-music. Hermetic: no GPU, no Docker daemon, no model.
#   ./qa.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 byte-compile =="
python3 -m compileall -q gx_music scripts tests

echo "== 2/4 shell syntax =="
for f in scripts/*.sh; do bash -n "$f" && echo "  ok  $f"; done

echo "== 3/4 unit + protocol integration tests =="
python3 -m unittest discover -s tests -t . 2>&1 | tail -4

echo "== 4/4 no literal credentials in the tree =="
if grep -rInE '(api[_-]?key|password|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' \
     --include='*.py' --include='*.sh' --include='*.service' --include='*.md' --include='Dockerfile' . \
     | grep -v 'KEY = "k" \* 40'; then
  echo "  FAIL literal credential found"; exit 1
fi
echo "  ok  none"
