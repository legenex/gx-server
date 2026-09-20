# DFlash2 compatibility overlay for vLLM GB10 0.28

## Why this exists

The production image `jstarkg/vllm-gb10-flashnext:0.28-sm121-r6`
(`vllm 0.1.dev20073+g8e685d198`) ships DFlash1 only:

- `DFlashQwen3Model` hardcodes `DFlashQwen3DecoderLayer(...)` when building
  layers (no `decoder_layer_cls` indirection).
- `DFlashQwen3ForCausalLM` hardcodes `DFlashQwen3Model(...)` (no `model_cls`).
- There is no `qwen3_dflash2.py`, no `DFlash2DraftModel` registry entry, and
  no DFlash2 worker/speculator.

`wyattearp/Qwen3.8-27B-DFlash2` is a genuine DFlash2 checkpoint. It carries
per-layer `attention_conv` / `mlp_conv` and a top-level `candidate_selector`.
Those tensors are legitimate architecture components. They must not be filtered
or deleted from the checkpoint.

Upstream vLLM later restored class indirection and added DFlash2. This overlay
backports the minimum of that work onto the immutable GB10 image at container
start so gx-reason can load the original intact draft weights.

## What the overlay does

1. Patches installed `qwen3_dflash.py` so the parent model constructs layers via
   `self.decoder_layer_cls(...)` and the causal LM via `self.model_cls(...)`.
2. Installs `qwen3_dflash2.py` (`DFlash2Qwen3DecoderLayer`,
   `DFlash2Qwen3Model`, `DFlash2Qwen3ForCausalLM`, grouped conv, candidate
   selector).
3. Registers `DFlash2DraftModel` → `DFlash2Qwen3ForCausalLM`.
4. Installs a DFlash2 speculator and routes `method=dflash` +
   `DFlash2DraftModel` to it.

The container image itself is not rebuilt. `run-vllm.sh` applies the overlay
into site-packages before `vllm serve`.

## Source

Derived from upstream vLLM `main` (`qwen3_dflash.py` decoder-layer indirection,
`qwen3_dflash2.py`, `v1/worker/gpu/spec_decode/dflash2/`) and adapted to APIs
present in the GB10 0.28 image (no `LogitsProcessor.get_top_k_tokens`, no
`gumbel_noised_argmax` helper).
