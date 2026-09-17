#!/usr/bin/env bash
# QA gate for gx-call. Hermetic: no GPU, no Docker daemon, no model.
#   ./qa.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 byte-compile (supervisor, engine, tests) =="
python3 -m compileall -q gx_call engine tests scripts

echo "== 2/5 shell syntax =="
for f in scripts/*.sh qa.sh; do [ -f "$f" ] && bash -n "$f" && echo "  ok  $f"; done

echo "== 3/5 unit + protocol integration tests (real supervisor, stub engine) =="
python3 -m unittest discover -s tests -t . 2>&1 | tail -4

echo "== 4/5 wildcard binds and GPU flags =="
if grep -rnE -- '--gpus|--runtime[= ]nvidia' gx_call engine scripts systemd 2>/dev/null | grep -v '^\S*:[0-9]*:#'; then
  echo "  FAIL use CDI (--device nvidia.com/gpu=all)"; exit 1
fi
echo "  ok"

echo "== 5/5 no literal credentials in the tree =="
if grep -rInE '(api[_-]?key|password|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' \
     --include='*.py' --include='*.sh' --include='*.service' --include='*.md' --include='Dockerfile' . \
     | grep -v 'KEY = "k" \* 40'; then
  echo "  FAIL literal credential found"; exit 1
fi
echo "  ok  none"
