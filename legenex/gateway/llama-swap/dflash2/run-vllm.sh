#!/bin/sh
# Pass DFlash2 speculative-config as one argv (JSON quotes must not be
# parsed by llama-swap's YAML/shell). vLLM 0.28 registers DFlashDraftModel,
# not upstream DFlash2DraftModel, so stage a patched config in /tmp.
# vLLM 0.28 DFlashQwen3Model has no candidate_selector; DFlash2 checkpoints
# ship one. Drop those tensors so the rest of the draft can load.
set -eu
SRC=/models/vllm/Qwen3.8-27B-DFlash2
DST=/tmp/dflash2-draft
SPEC_FILE=""
for p in /opt/dflash2/spec.json /models/dflash2/spec.json; do
  if [ -f "$p" ]; then
    SPEC_FILE=$p
    break
  fi
done
if [ -z "$SPEC_FILE" ]; then
  echo "dflash spec.json missing" >&2
  exit 1
fi
mkdir -p "$DST"
cp "$SRC/config.json" "$DST/config.json"
python3 - <<'PY'
import json
from pathlib import Path

from safetensors import safe_open
from safetensors.torch import save_file

cfg_path = Path("/tmp/dflash2-draft/config.json")
cfg = json.loads(cfg_path.read_text())
cfg["architectures"] = ["DFlashDraftModel"]
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")

src = "/models/vllm/Qwen3.8-27B-DFlash2/model.safetensors"
dst = "/tmp/dflash2-draft/model.safetensors"
kept = {}
dropped = []
with safe_open(src, framework="pt", device="cpu") as fh:
    for key in fh.keys():
        if key.startswith("candidate_selector"):
            dropped.append(key)
            continue
        kept[key] = fh.get_tensor(key)
save_file(kept, dst)
print(f"dflash draft staged keys={len(kept)} dropped={dropped}", flush=True)
PY
SPEC=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["model"]="/tmp/dflash2-draft"; print(json.dumps(d, separators=(",",":")))' "$SPEC_FILE")
exec vllm serve "$@" --speculative-config "$SPEC"
