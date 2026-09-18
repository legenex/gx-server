#!/bin/sh
# Pass DFlash2 speculative-config as one argv (JSON quotes must not be
# parsed by llama-swap's YAML/shell). vLLM does not treat a file path as JSON.
set -eu
SPEC=$(cat /models/dflash2/spec.json)
exec vllm serve "$@" --speculative-config "$SPEC"
