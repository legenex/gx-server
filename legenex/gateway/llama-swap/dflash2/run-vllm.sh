#!/bin/sh
# DFlash2 is disabled on the production vLLM 0.28 GB10 image.
# wyattearp/Qwen3.8-27B-DFlash2 ships candidate_selector and per-layer
# attention_conv (dflash_config.selector_rank=256, conv_kernel_size=2).
# vLLM 0.28 DFlashQwen3Model implements neither; draft load fails before
# serve. Do not strip those tensors — the remaining weights are not a
# compatible DFlash draft. Serve the target model without speculation.
set -eu
echo "gx-reason: DFlash disabled (vLLM 0.28 DFlashQwen3Model incompatible with DFlash2 checkpoint)" >&2
exec vllm serve "$@"
