#!/usr/bin/env bash
# QA gate for the gx-media stack. Runs on node 1; needs no GPU and no ComfyUI.
#   ./qa.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 byte-compile =="
python3 -m compileall -q gx_media_router

echo "== 2/5 workflow templates parse and validate =="
python3 - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, ".")
from gx_media_router.workflows import WorkflowRegistry
registry = WorkflowRegistry(Path("../workflows"))
for name in registry.names():
    workflow = registry.get(name)
    print(f"  ok  {name:34s} kind={workflow.kind:5s} nodes={len(workflow.graph):2d} "
          f"bindings={len(workflow.bindings)}")
PY

echo "== 3/5 compose file is valid YAML and pins no secrets =="
python3 - <<'PY'
import re, sys
text = open("../docker-compose.media.yml").read()
try:
    import yaml; yaml.safe_load(text); print("  ok  YAML parses")
except ImportError:
    print("  --  pyyaml absent; skipped structural parse")
bad = re.findall(r'(?i)(api_key|password|token|secret)\s*[:=]\s*["\']?(?!\$\{|os\.environ)[A-Za-z0-9_\-]{12,}', text)
if bad:
    print("  FAIL literal credential in compose:", bad); sys.exit(1)
print("  ok  no literal credentials")
PY

echo "== 4/5 unit + protocol integration tests =="
python3 -m unittest discover -s tests -t . -v 2>&1 | tail -5

echo "== 5/5 secret scan of the media tree =="
if grep -rInE "(sk-[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16})" .. ; then
  echo "  FAIL possible secret found"; exit 1
fi
echo "  ok  no secrets found"

echo
echo "QA PASSED"
