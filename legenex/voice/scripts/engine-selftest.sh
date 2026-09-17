#!/usr/bin/env bash
# Runtime self-test of the gx-voice engine image ON THE GPU (gx10-02).
# Checks what the build cannot: CUDA visible, Blackwell kernels present, a real
# bf16 matmul runs, and qwen_tts imports. Uses < 2 GiB. Started through the
# node-2 admission guard (BUILD_V3 rule 2), never with a bare docker run.
set -euo pipefail
IMAGE="${1:-gx-voice-engine:qwen3tts-022e286-t214}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if docker inspect gx-max-rank1 >/dev/null 2>&1 || docker inspect gx-max-rank0 >/dev/null 2>&1; then
  echo "REFUSED: gx-max is present on this node" >&2; exit 2
fi
# shellcheck source=/dev/null
source "${here}/../../lifecycle/resource-guard.sh"
name="gx-voice-selftest-$$"
gx_guard_run node2 "${name}" small 2 -- \
  docker run -d --name "${name}" --device nvidia.com/gpu=all --network none --memory 4g \
    --entrypoint python "${IMAGE}" -c '
import torch
assert torch.cuda.is_available(), "CUDA not available"
al = torch.cuda.get_arch_list()
print("device", torch.cuda.get_device_name(0), "capability", torch.cuda.get_device_capability(0), "arch", al)
assert any(a in al for a in ("sm_120", "sm_121")), "no Blackwell kernels"
x = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
print("matmul ok", round((x @ x.T).float().sum().item(), 2))
from qwen_tts import Qwen3TTSModel
print("selftest OK")
' >/dev/null
rc=0
docker wait "${name}" >/dev/null || rc=$?
docker logs "${name}" 2>&1 | grep -v -e '^\*\*\*' -e 'flash-attn' -e '^ *$' || true
status="$(docker inspect -f '{{.State.ExitCode}}' "${name}" 2>/dev/null || echo 1)"
docker rm -f "${name}" >/dev/null 2>&1 || true
gx_guard_release node2 "${name}" >/dev/null 2>&1 || true
[ "${rc}" -eq 0 ] && [ "${status}" = 0 ]
