"""GX vLLM general plugin: prove WHICH copy of attn.wo_b each TP rank loaded.

The abliterated checkpoint is an overlay: 92 ``*.attn.wo_b.{weight,scale}``
tensors exist twice on disk (original bytes inside the 48 base shards, edited
bytes in ``model-overlay-00001-of-00001.safetensors``). vLLM yields every
tensor of every indexed file and the last write wins, so the overlay must be
read last. This plugin turns that from an argument into a measurement.

When ``GX_WOB_DIGEST=1`` it wraps ``DefaultModelLoader.load_weights`` and,
right after the raw checkpoint load and BEFORE ``process_weights_after_loading``
can repack anything, logs one line per probed parameter::

    GX_WOB_DIGEST {"model_class": ..., "param": ..., "tp_rank": 0, "tp_size": 2,
                   "shape": [4096, 4096], "dtype": "torch.float8_e4m3fn",
                   "sha256": "..."}

``tools/overlay_check.py digests`` computes the matching per-rank digests of
the overlay copy and of the base copy from the files on disk, so the
acceptance run can say "rank r holds the overlay bytes" or "rank r holds the
ORIGINAL bytes" with nothing in between.

It never raises into vLLM: any failure is logged and loading continues.
Without GX_WOB_DIGEST=1 it does nothing at all.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

log = logging.getLogger("gx.wob_probe")

#: vLLM parameter names probed: the first decoder layer of the target model
#: and of the DSpark draft model (whose mtp.0.* stage is model.layers.0.*).
_PROBE = re.compile(r"(^|\.)layers\.0\.attn\.wo_b\.weight$")
_installed = False


def _digest(tensor) -> str:
    import torch  # noqa: PLC0415 - only inside vLLM processes

    flat = tensor.detach().contiguous().reshape(-1)
    raw = flat.view(torch.uint8).cpu().numpy().tobytes()
    return hashlib.sha256(raw).hexdigest()


def _report(model) -> None:
    try:
        from vllm.distributed import (  # noqa: PLC0415
            get_tensor_model_parallel_rank,
            get_tensor_model_parallel_world_size,
        )

        rank = get_tensor_model_parallel_rank()
        size = get_tensor_model_parallel_world_size()
    except Exception:  # noqa: BLE001 - no TP group (unit use)
        rank, size = 0, 1
    for name, param in model.named_parameters():
        if not _PROBE.search(name):
            continue
        try:
            line = {
                "model_class": type(model).__name__,
                "param": name,
                "tp_rank": rank,
                "tp_size": size,
                "shape": list(param.shape),
                "dtype": str(param.dtype),
                "sha256": _digest(param.data),
            }
            # print() as well as log: the line must reach `docker logs` even if
            # vLLM's logging config filters third-party loggers.
            text = "GX_WOB_DIGEST " + json.dumps(line, separators=(",", ":"))
            print(text, flush=True)
            log.info(text)
        except Exception as exc:  # noqa: BLE001
            print(f"GX_WOB_DIGEST_ERROR {name}: {exc!r}", flush=True)


def register() -> None:
    """vLLM calls this in every process (API server, engine core, workers)."""
    global _installed
    if _installed or os.environ.get("GX_WOB_DIGEST", "0") != "1":
        return
    try:
        from vllm.model_executor.model_loader.default_loader import (  # noqa: PLC0415
            DefaultModelLoader,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"GX_WOB_DIGEST_ERROR cannot import DefaultModelLoader: {exc!r}", flush=True)
        return
    original = DefaultModelLoader.load_weights

    def load_weights(self, model, model_config):  # noqa: ANN001
        result = original(self, model, model_config)
        _report(model)
        return result

    load_weights.__wrapped__ = original  # type: ignore[attr-defined]
    DefaultModelLoader.load_weights = load_weights
    _installed = True
