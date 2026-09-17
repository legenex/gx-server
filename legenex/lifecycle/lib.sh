#!/usr/bin/env bash
# Shared helpers for gx-max lifecycle scripts.
#
# NOTE: this library deliberately does NOT set -e. It is sourced by both
# fail-fast scripts (start/stop, which set their own -euo pipefail) and by
# tolerant ones (status, which must keep reporting when a probe fails).

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_here}/gx-max.conf"

log() { printf '[%s] %s\n' "$(date -Is)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

n2() { ssh -o BatchMode=yes -o ConnectTimeout=10 "${GXMAX_NODE2_SSH}" "$@"; }

# The SGLang argument vector, shared by both ranks. Only --node-rank differs.
#
# The LOCKED parts (engine, --model-path, --tp 2, --nnodes 2, the DSV4/b12x
# backend selection) are literals here and must stay literals; the MoE runner
# follows the official cookbook cell for the checkpoint (GXMAX_QUANT_CELL). The memory
# sizing values come from gx-max.conf, whose defaults are the verified 4b96e49
# / official-cookbook values (D-025). With the defaults this emits the 4b96e49
# vector plus --enable-metrics (D-002); only the flag ORDER differs, because
# --model-loader-extra-config is emitted last.
gxmax_args() {
  local rank="$1"
  printf '%s\n' \
    sglang serve \
    --trust-remote-code \
    --model-path /model \
    --tp 2 \
    --nnodes 2 \
    --node-rank "${rank}" \
    --dist-init-addr "${GXMAX_DIST_ADDR}"
  # MoE runner selection: the ONLY difference between the two official DGX
  # Spark cookbook cells (sgl-project/sglang deepseek-v4.jsx, 2026-09-17).
  case "${GXMAX_QUANT_CELL:-fp4}" in
    fp4)
      printf '%s\n' --moe-runner-backend b12x ;;
    nvfp4)
      printf '%s\n' --moe-runner-backend flashinfer_cutlass \
        --speculative-moe-runner-backend b12x \
        --disable-shared-experts-fusion ;;
    *)
      echo "gx-max.conf: unknown GXMAX_QUANT_CELL '${GXMAX_QUANT_CELL}'" >&2
      return 1 ;;
  esac
  printf '%s\n' \
    --speculative-algorithm DSPARK \
    --weight-loader-drop-cache-after-load \
    --startup-weight-load-mode serial \
    --chunked-prefill-size "${GXMAX_CHUNKED_PREFILL_SIZE}" \
    --context-length "${GXMAX_CONTEXT_LENGTH}" \
    --mem-fraction-static "${GXMAX_MEM_FRACTION_STATIC}" \
    --swa-full-tokens-ratio 0.2 \
    --cuda-graph-max-bs-decode "${GXMAX_CUDA_GRAPH_MAX_BS_DECODE}" \
    --max-running-requests "${GXMAX_MAX_RUNNING_REQUESTS}" \
    --host 0.0.0.0 \
    --port "${GXMAX_PORT}"
  if [ "${GXMAX_ENABLE_METRICS:-0}" = "1" ]; then
    printf '%s\n' --enable-metrics
  fi
  if [ "${GXMAX_DISABLE_WEIGHT_MMAP:-0}" = "1" ]; then
    printf '%s\n' --weight-loader-disable-mmap
  fi
  if [ -n "${GXMAX_LOAD_FORMAT:-}" ]; then
    printf '%s\n' --load-format "${GXMAX_LOAD_FORMAT}"
  fi
  # --model-loader-extra-config is LOADER config, and each --load-format
  # accepts a different set of keys: runai_streamer rejects
  # {"enable_multithread_load":false} outright ("Unexpected extra config keys
  # for load format LoadFormat.RUNAI_STREAMER"). Emitting it only when it is
  # non-empty keeps the default loader's verified-working setting while
  # letting an alternative loader be selected without a contradictory flag.
  if [ -n "${GXMAX_LOADER_EXTRA_CONFIG:-}" ]; then
    printf '%s\n' --model-loader-extra-config "${GXMAX_LOADER_EXTRA_CONFIG}"
  fi
}

# In-flight request count. Returns a number, or the string "unknown" when the
# metrics endpoint is not exposed (GXMAX_ENABLE_METRICS=0 on the running server).
gxmax_inflight() {
  local body
  body=$(curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/metrics" 2>/dev/null) || { echo unknown; return; }
  printf '%s\n' "$body" | awk '
    /^sglang:num_running_reqs/{r=$2}
    /^sglang:num_queue_reqs/{q=$2}
    END{ printf "%d", r+q }'
}

# Environment flags required for the DSV4 / SM121 path. Load-time OOM mitigation
# lives in the argument vector above; these are the kernel-selection switches.
gxmax_env_flags() {
  printf '%s\n' \
    -e SGLANG_SM120_FLASHMLA_BACKEND=b12x \
    -e B12X_MLA_SM120_DSV4_H16_NATIVE=1 \
    -e SGLANG_OPT_FUSE_MHC_POST_PRE=1 \
    -e SGLANG_OPT_FP8_WO_A_GEMM=1 \
    -e SGLANG_SKIP_SGL_KERNEL_VERSION_CHECK=1 \
    -e "SGLANG_B12X_MAX_TOKENS=${GXMAX_CHUNKED_PREFILL_SIZE}" \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
}

# Docker flags common to both ranks. GPU access is via CDI (nvidia.com/gpu=all);
# /dev/infiniband + CAP_IPC_LOCK + unlimited memlock are required for RoCE.
#
# There is deliberately NO --memory / --memory-swap (D-025). An equal pair
# sets the container's memory.swap.max to 0, which stopped the loader's
# staging pages from spilling into /swapfile-sglang and turned a survivable
# load transient into a kernel OOM kill. --oom-score-adj stays: it makes
# SGLang, not sshd/tailscaled, the kernel's victim of last resort.
gxmax_docker_flags() {
  printf '%s\n' \
    --network host \
    --ipc host \
    --shm-size "${GXMAX_SHM_BYTES}" \
    --device nvidia.com/gpu=all \
    --device /dev/infiniband \
    --cap-add IPC_LOCK \
    --ulimit memlock=-1:-1 \
    --oom-score-adj "${GXMAX_OOM_SCORE_ADJ}" \
    -v "${GXMAX_MODEL_DIR}:/model:ro" \
    -v "${GXMAX_CACHE_DIR}:/root/.cache"
}

gxmax_healthy() {
  curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/health" >/dev/null 2>&1
}

gxmax_rank0_running() { [ "$(docker inspect -f '{{.State.Running}}' "${GXMAX_RANK0_NAME}" 2>/dev/null || echo false)" = "true" ]; }
gxmax_rank1_running() { [ "$(n2 "docker inspect -f '{{.State.Running}}' ${GXMAX_RANK1_NAME} 2>/dev/null || echo false")" = "true" ]; }

# D-039: one line with the engine's own startup breakdown, parsed from the
# rank0 log after READY (observability only; never affects a decision).
#   gxmax_engine_phases <rank0-log> <rank0-launch-epoch> <ready-epoch>
gxmax_engine_phases() {
  python3 - "$1" "$2" "$3" <<'PY' 2>/dev/null || true
import re, sys, datetime as dt
path, launched, ready = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
stamp = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
def epoch(line):
    m = stamp.match(line)
    return dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() if m else None
first = weights = draft = graphs = dist = serving = None
out = {}
try:
    lines = open(path, errors="replace").read().splitlines()
except OSError:
    sys.exit(0)
for line in lines:
    t = epoch(line)
    if t is None:
        continue
    if first is None and "server_args=" in line:
        first = t
        out["container_to_args"] = t - launched
    m = re.search(r"Init torch distributed ends\. elapsed=([\d.]+)", line)
    if m and "dist_init" not in out:
        out["dist_init"] = float(m.group(1))
    m = re.search(r"Load weight end\. elapsed=([\d.]+)", line)
    if m:
        out["draft_weights" if "weights" in out else "weights"] = float(m.group(1))
    m = re.search(r"Capture .*CUDA graph end\. elapsed=([\d.]+)", line)
    if m:
        out["cuda_graphs"] = out.get("cuda_graphs", 0.0) + float(m.group(1))
    if "Uvicorn running" in line and "http_up" not in out:
        out["http_up"] = t - launched
    if "fired up and ready to roll" in line and "warmup_done" not in out:
        out["warmup_done"] = t - launched
out["health_ready"] = ready - launched
print("ENGINE_PHASES " + " ".join(f"{k}={v:.1f}" for k, v in out.items()))
PY
}
