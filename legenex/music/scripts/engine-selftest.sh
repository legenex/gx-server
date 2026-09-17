#!/usr/bin/env bash
# Runtime self-test of the gx-music engine image ON THE GPU (node 2).
# Checks what cannot be checked at build time: CUDA visible, Blackwell cubins
# present, and a real CUDA kernel runs. Uses <2 GiB; safe next to other work.
set -euo pipefail
IMAGE="${1:-gx-music-engine:acestep15-ca1e85f-t214}"
# Never touch the GPU while gx-max owns the node (2026-09-17 02:10 lesson:
# even a 2 GiB context allocation fails and adds NVRM noise to gx-max's
# safety counters).
if docker inspect gx-max-rank1 >/dev/null 2>&1 || docker inspect gx-max-rank0 >/dev/null 2>&1; then
  echo "REFUSED: gx-max is present on this node" >&2; exit 2
fi
avail_gib=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
if [ "${avail_gib}" -lt 40 ]; then
  echo "REFUSED: only ${avail_gib} GiB MemAvailable (need >= 40)" >&2; exit 2
fi
docker run --rm --device nvidia.com/gpu=all --network none --entrypoint python "${IMAGE}" -c '
import torch
assert torch.cuda.is_available(), "CUDA not available"
al = torch.cuda.get_arch_list()
cap = torch.cuda.get_device_capability(0)
print("device", torch.cuda.get_device_name(0), "capability", cap, "arch", al)
assert any(a in al for a in ("sm_120", "sm_121")), "no Blackwell cubin"
x = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
y = (x @ x.T).float().sum().item()
print("matmul ok", round(y, 2))
import acestep.handler, acestep.llm_inference
print("selftest OK")
'
