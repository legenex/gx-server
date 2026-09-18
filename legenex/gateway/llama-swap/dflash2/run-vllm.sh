#!/bin/sh
# Pass DFlash2 speculative-config as one argv (JSON quotes must not be
# parsed by llama-swap's YAML/shell). vLLM 0.28 registers DFlashDraftModel,
# not upstream DFlash2DraftModel, so stage a patched config in /tmp.
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
python3 -c 'import json; from pathlib import Path; p=Path("/tmp/dflash2-draft/config.json"); d=json.loads(p.read_text()); d["architectures"]=["DFlashDraftModel"]; p.write_text(json.dumps(d, indent=2)+"\n")'
ln -sfn "$SRC/model.safetensors" "$DST/model.safetensors"
SPEC=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["model"]="/tmp/dflash2-draft"; print(json.dumps(d, separators=(",",":")))' "$SPEC_FILE")
exec vllm serve "$@" --speculative-config "$SPEC"
