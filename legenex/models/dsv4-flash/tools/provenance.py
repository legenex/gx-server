#!/usr/bin/env python3
"""Print the installed versions of the runtime's load-bearing packages (image provenance)."""
import json
from importlib import metadata

WANT = ("vllm", "flashinfer-python", "flashinfer-cubin", "flashinfer-jit-cache", "b12x", "torch",
        "cuda-python", "cuda-bindings", "nvidia-cutlass-dsl", "nvidia-cutlass-dsl-libs-base",
        "nvidia-cutlass-dsl-libs-cu13", "apache-tvm-ffi", "gx-wob-probe", "transformers",
        "safetensors", "nvidia-nccl-cu13")

out = {}
for name in WANT:
    try:
        out[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        out[name] = None
print(json.dumps(out, indent=1, sort_keys=True))
