#!/usr/bin/env bash
# QA gate for gx-live. Hermetic: no GPU, no Docker daemon, no model.
#   ./qa.sh
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")"
ui=../control-ui

echo "== 1/5 byte-compile =="
python3 -m compileall -q gx_live tests engine/gx_live_engine scripts

echo "== 2/5 lint (ruff) =="
if [ -x "${ui}/.venv/bin/ruff" ]; then
  "${ui}/.venv/bin/ruff" check --config "${ui}/pyproject.toml" gx_live tests engine/gx_live_engine scripts
else
  echo "  skip: ${ui}/.venv/bin/ruff not installed on this host"
fi

echo "== 3/5 shell syntax =="
for f in scripts/*.sh; do [ -e "$f" ] && bash -n "$f" && echo "  ok  $f"; done

echo "== 4/5 unit + protocol integration tests =="
python3 -m unittest discover -s tests -t . 2>&1 | tail -3
# engine logic needs numpy: the engine image (gx10-02) or legenex/live/.venv (python3 -m venv .venv &&
# .venv/bin/pip install numpy==2.3.5)
if python3 -c "import numpy" 2>/dev/null; then
  python3 -m unittest discover -s engine/tests -t engine 2>&1 | tail -3
elif [ -x .venv/bin/python ]; then
  .venv/bin/python -m unittest discover -s engine/tests -t engine 2>&1 | tail -3
elif docker image inspect gx-live-engine:minicpmo45-503e754-t214 >/dev/null 2>&1; then
  docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD/engine:/t:ro" -w /t \
    --entrypoint python gx-live-engine:minicpmo45-503e754-t214 -m unittest discover -s tests -t . 2>&1 | tail -3
else
  echo "  FAIL: no numpy for the engine tests (create legenex/live/.venv)"; exit 1
fi

echo "== 5/5 no literal credentials in the tree =="
if grep -rInE '(api[_-]?key|password|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' \
     --include='*.py' --include='*.sh' --include='*.service' --include='*.md' --include='Dockerfile' . ; then
  echo "  FAIL literal credential found"; exit 1
fi
echo "  ok  none"
echo "QA PASSED"
