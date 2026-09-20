#!/bin/sh
# Pass DFlash2 speculative-config as one argv (JSON quotes must not be
# parsed by llama-swap's YAML/shell).
#
# vLLM GB10 0.28 image ships DFlash1 only and hardcodes decoder layers.
# Apply the repo overlay so DFlash2Qwen3Model builds DFlash2Qwen3DecoderLayer
# and can load the intact checkpoint (attention_conv + candidate_selector).
# NEVER filter or rewrite checkpoint tensors.
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

# Compatibility overlay (canonical repo path mounted at /opt/dflash2).
if [ -f /opt/dflash2/overlay/apply.py ]; then
  echo "gx-reason: applying DFlash2 vLLM overlay" >&2
  python3 /opt/dflash2/overlay/apply.py
else
  echo "gx-reason: DFlash2 overlay missing at /opt/dflash2/overlay/apply.py" >&2
  exit 1
fi

mkdir -p "$DST"
# Stage config only: rewrite architecture to DFlash2DraftModel so the registry
# selects DFlash2Qwen3ForCausalLM. Weights stay the ORIGINAL intact file via
# symlink — no tensor filtering, no copy of the multi-GB safetensors.
cp "$SRC/config.json" "$DST/config.json"
python3 - <<'PY'
import json
from pathlib import Path

p = Path("/tmp/dflash2-draft/config.json")
cfg = json.loads(p.read_text())
# Intact DFlash2 checkpoint; do not strip candidate_selector / attention_conv.
cfg["architectures"] = ["DFlash2DraftModel"]
p.write_text(json.dumps(cfg, indent=2) + "\n")
print("dflash2 draft staged architecture=DFlash2DraftModel (intact weights symlink)", flush=True)
PY
ln -sfn "$SRC/model.safetensors" "$DST/model.safetensors"
# Preserve optional sidecar files if present (mask embedding, etc.).
for f in "$SRC"/*; do
  base=$(basename "$f")
  case "$base" in
    config.json|model.safetensors) ;;
    *)
      if [ -f "$f" ] && [ ! -e "$DST/$base" ]; then
        ln -sfn "$f" "$DST/$base"
      fi
      ;;
  esac
done

# Prove symlink targets the original multi-GB file.
sz=$(wc -c <"$DST/model.safetensors")
echo "dflash2 draft weight bytes=$sz (expect 3848817896)" >&2
if [ "$sz" -lt 3000000000 ]; then
  echo "dflash2 draft weight too small — refusing filtered/corrupt checkpoint" >&2
  exit 1
fi

SPEC=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["model"]="/tmp/dflash2-draft"; print(json.dumps(d, separators=(",",":")))' "$SPEC_FILE")
echo "gx-reason: starting vLLM with DFlash2 speculative-config" >&2
exec vllm serve "$@" --speculative-config "$SPEC"
