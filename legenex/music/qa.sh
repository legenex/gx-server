#!/usr/bin/env bash
# QA gate for gx-music. Hermetic: no GPU, no Docker daemon, no model.
#   ./qa.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 byte-compile =="
python3 -m compileall -q -x "analysis_dsp" gx_music scripts tests && python3 -m py_compile gx_music/analysis_dsp.py

echo "== 2/5 shell syntax =="
for f in scripts/*.sh; do bash -n "$f" && echo "  ok  $f"; done

echo "== 3/5 unit + protocol integration tests =="
python3 -m unittest discover -s tests -t . 2>&1 | tail -4

echo "== 4/5 measured-analysis (DSP) tests =="
if python3 -c 'import numpy, scipy' 2>/dev/null; then
  python3 -m unittest tests.test_analysis_dsp 2>&1 | tail -3
elif command -v docker >/dev/null 2>&1 && docker image inspect "${GX_MUSIC_IMAGE:-gx-music-engine:acestep15-ca1e85f-t214}" >/dev/null 2>&1; then
  scripts/dsp-selftest.sh 2>&1 | tail -3
else
  echo "  SKIPPED here (no numpy/scipy and no engine image): run scripts/dsp-selftest.sh on gx10-02"
fi

echo "== 5/5 no literal credentials in the tree =="
if grep -rInE '(api[_-]?key|password|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' \
     --include='*.py' --include='*.sh' --include='*.service' --include='*.md' --include='Dockerfile' . \
     | grep -v 'KEY = "k" \* 40'; then
  echo "  FAIL literal credential found"; exit 1
fi
echo "  ok  none"
