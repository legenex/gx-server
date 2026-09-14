#!/usr/bin/env bash
# ==============================================================================
# gx-reason-diagnose.sh — B-011 root-cause split test: CUDA/kernel bug vs a bad
# quant/model, WITHOUT repeating the B-012 incident that this exact comparison
# caused last time.
#
# Background (coordination/BLOCKERS.md B-011, B-012; TEST_RESULTS.md section 7
# and 8; coordination/DECISIONS.md D-009):
#   gx-reason (unsloth/Qwen3.5-122B-A10B-GGUF, UD-Q4_K_XL, llama.cpp, node 2)
#   loads cleanly and generates at a normal rate, but every token is garbage:
#     curl .../completion -d '{"prompt":"The capital of France is",
#                              "n_predict":20,"temperature":0}'
#     -> {"content":"////////////////////", ...}
#   This is NOT a chat-template problem (raw /completion fails identically to
#   /v1/chat/completions) and NOT a memory problem (the whole 77 GB model is
#   file-backed mmap with 56 GiB still free). Two hypotheses remain open:
#     (1) a CUDA/GDN kernel bug in this llama.cpp build's qwen3_5_moe hybrid
#         (linear-attention/GDN + full-attention) implementation, or
#     (2) a bad dynamic quant / a stale llama.cpp build, unrelated to CUDA.
#   The previous attempt to distinguish these with a `--n-gpu-layers 0` run
#   started a SECOND 77 GB mmap model while the first was still resident,
#   which put node 2 into sustained mmap thrashing (B-012): the kernel kept
#   answering ICMP but userspace starved and SSH could not complete its
#   banner exchange. It required a physical power cycle.
#
# This script is the SAFE version of that exact comparison. It differs from
# the failed attempt in the one way that matters:
#   * it unloads gx-reason (and waits for the memory to actually come back)
#     BEFORE starting the CPU-only run, and never has both resident at once;
#   * it re-checks free memory before EACH heavy load, not just once;
#   * every remote step has a hard wall-clock timeout, so a still-unhealthy
#     node fails this script fast instead of hanging it.
#
# What it does, in order:
#   1. Preflight: node2 reachable, enough free memory to even attempt this.
#   2. Unload gx-reason via llama-swap's API (documented in BLOCKERS.md B-012
#      and legenex/gateway/README.md), and CONFIRM memory actually came back
#      before proceeding -- "asked it to unload" is not the same as "unloaded".
#   3. Run 1 (GPU path): trigger gx-reason's normal, already-configured
#      llama-swap-managed service (--n-gpu-layers 99, per
#      legenex/gateway/llama-swap/node02.yaml) with the diagnostic prompt at
#      temperature 0, capture the raw text.
#   4. Unload gx-reason again and CONFIRM memory came back.
#   5. Run 2 (CPU-only path): start a STANDALONE, differently-named container
#      (never touching llama-swap's managed "gx-reason" name/port) using the
#      exact same image, weights (read-only) and sampling params, but
#      --n-gpu-layers 0 and no GPU device attached at all -- the most
#      rigorous way to force a true CPU path. Bound to node2's own loopback
#      only (never the fabric or LAN), queried via SSH exec, matching this
#      repo's "never expose an unauthenticated llama.cpp endpoint" posture.
#   6. Classify both outputs with an OBJECTIVE, code-driven test for
#      "sane" vs "garbage" -- no human eyeballing required.
#   7. Report a diagnosis (which hypothesis the evidence supports) and clean
#      up: stop the diagnostic container, unload gx-reason, leave node 2 idle.
#
# This script must NOT be run against node 2 while it is not known-healthy.
# Its own preflight (SSH + free-memory gates before every heavy step) will
# refuse to proceed if that is not the case -- but it is written, reviewed,
# and syntax-checked WITHOUT ever being executed against a live node 2.
#
# Usage:
#   legenex/scripts/gx-reason-diagnose.sh
#   legenex/scripts/gx-reason-diagnose.sh --prompt "The capital of France is" \
#       --n-predict 20
#
# Requires GX_SWAP_API_KEY in the environment (same value used by the
# gateway -- see legenex/gateway/.env). Never hardcode it, never log it.
# Exit code: 0 = both runs completed and were classified; non-zero = a
# precondition failed or a run could not be completed (see log for which).
# ==============================================================================
set -uo pipefail

# ------------------------------------------------------------------- config --
NODE2_SSH="${GX_NODE2_SSH:-legenex-02@gx10-02}"
NODE2_FABRIC_A="${GX_NODE2_FABRIC_A:-192.168.100.11}"
SWAP_PORT="${GX_SWAP_PORT:-28080}"
GX_SWAP_BASE="http://${NODE2_FABRIC_A}:${SWAP_PORT}"
SSH_CONNECT_TIMEOUT="${GX_SSH_CONNECT_TIMEOUT:-10}"
SSH_HARD_TIMEOUT="${GX_SSH_HARD_TIMEOUT:-20}"

PROMPT="${GX_DIAG_PROMPT:-The capital of France is}"
N_PREDICT="${GX_DIAG_N_PREDICT:-20}"
MIN_FREE_GIB="${GX_DIAG_MIN_FREE_GIB:-90}"          # same threshold gx-max-start.sh uses
UNLOAD_WAIT_TRIES="${GX_DIAG_UNLOAD_WAIT_TRIES:-30}" # 30 * 5s = 150s budget to reclaim memory
GPU_LOAD_TIMEOUT="${GX_DIAG_GPU_LOAD_TIMEOUT:-1800}" # matches node02.yaml healthCheckTimeout
CPU_LOAD_TIMEOUT="${GX_DIAG_CPU_LOAD_TIMEOUT:-1800}" # CPU-only load of a 77GB mmap can be slow

CPU_PORT="${GX_DIAG_CPU_PORT:-25900}"
CPU_CONTAINER="gx-reason-diag-cpu"
GGUF_IMAGE="legenex/llama-cpp-spark:latest"
MODEL_FILE="/models/gguf/Qwen3.5-122B-A10B/UD-Q4_K_XL/Qwen3.5-122B-A10B-UD-Q4_K_XL-00001-of-00003.gguf"
MODELS_ROOT="/srv/models"

: "${GX_SWAP_API_KEY:?GX_SWAP_API_KEY must be set in the environment (same value as legenex/gateway/.env) -- refusing to guess or hardcode a credential}"

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prompt) PROMPT="$2"; shift ;;
    --n-predict) N_PREDICT="$2"; shift ;;
    --min-free-gib) MIN_FREE_GIB="$2"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

ts() { date -Is; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

remote() {
  timeout "${SSH_HARD_TIMEOUT}" ssh -o BatchMode=yes \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "${NODE2_SSH}" "$@"
}

# A separate, generous-timeout wrapper for remote calls that are expected to
# take a long time by design (waiting for a 77GB mmap load), as opposed to
# `remote`'s tight bound for quick status checks.
remote_slow() {
  local budget="$1"; shift
  timeout "${budget}" ssh -o BatchMode=yes \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=6 \
    "${NODE2_SSH}" "$@"
}

CPU_CONTAINER_STARTED=0

cleanup() {
  local rc=$?
  log "=== cleanup: leaving node2 idle regardless of outcome ==="
  if [ "${CPU_CONTAINER_STARTED}" -eq 1 ]; then
    remote "docker rm -f ${CPU_CONTAINER} >/dev/null 2>&1" || true
    log "removed diagnostic container ${CPU_CONTAINER} (best effort)"
  fi
  curl -fsS -m 15 -X POST -H "Authorization: Bearer ${GX_SWAP_API_KEY}" \
    "${GX_SWAP_BASE}/api/models/unload/gx-reason" >/dev/null 2>&1 || true
  log "requested gx-reason unload (best effort) -- node2 should be idle"
  exit "${rc}"
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------- objective test
# Deterministic, code-driven "sane vs garbage" classifier. No human judgement
# required. Mirrors exactly the failure mode already on record for B-011
# (a single repeated non-alphanumeric character), while also catching other
# obviously-degenerate outputs.
classify_output() {
  python3 - "$1" <<'PY'
import re
import sys
import itertools

text = sys.argv[1]
stripped = text.strip()
if not stripped:
    print("GARBAGE empty_response")
    raise SystemExit

no_space = re.sub(r"\s+", "", stripped)
distinct = set(no_space)

if len(distinct) <= 2 and not any(c.isalnum() for c in distinct):
    print(f"GARBAGE repeated_nonalnum_char:{no_space[:3]!r}")
    raise SystemExit

longest = max((len(list(g)) for _, g in itertools.groupby(no_space)), default=0)
if no_space and longest / len(no_space) >= 0.6:
    print(f"GARBAGE dominated_by_one_repeated_char:{longest}/{len(no_space)}")
    raise SystemExit

words = re.findall(r"[A-Za-z]{2,}", stripped)
if len(words) < 2:
    print(f"GARBAGE too_few_real_words:{len(words)}")
    raise SystemExit

alnum = sum(1 for c in no_space if c.isalnum())
ratio = (alnum / len(no_space)) if no_space else 0.0
if ratio < 0.5:
    print(f"GARBAGE low_alnum_ratio:{ratio:.2f}")
    raise SystemExit

print(f"SANE words={len(words)} alnum_ratio={ratio:.2f}")
PY
}

# ----------------------------------------------------------------- preflight
mem_available_gib() {
  remote awk "'/MemAvailable/{print int(\$2/1048576)}'" /proc/meminfo 2>/dev/null
}

require_mem_free() {
  local avail
  avail=$(mem_available_gib)
  if [ -z "${avail}" ]; then
    log "could not read node2 memory availability"
    return 1
  fi
  log "node2 MemAvailable = ${avail} GiB (need >= ${MIN_FREE_GIB} GiB)"
  [ "${avail}" -ge "${MIN_FREE_GIB}" ]
}

preflight() {
  log "=== preflight ==="
  if ! remote true 2>/dev/null; then
    die "node2 is not reachable over SSH (legenex-02@gx10-02). Do not proceed. This script never talks to the fabric address for SSH -- that is model-traffic only."
  fi
  log "ssh reachable"

  local kernel
  kernel=$(remote uname -r 2>/dev/null)
  [ "${kernel}" = "6.17.0-1032-nvidia" ] || die "unexpected kernel on node2: '${kernel}' (expected 6.17.0-1032-nvidia, ARCHITECTURE.md L-4). Refusing to load anything until this is fixed."
  log "kernel ok: ${kernel}"

  require_mem_free || die "not enough free memory on node2 to safely start this diagnostic (need >= ${MIN_FREE_GIB} GiB free right now). Something else may already be resident -- check 'docker ps' on node2 before retrying. This is exactly the guard that would have prevented B-012."
  log "preflight passed"
}

# --------------------------------------------------------------- unload/wait
unload_gx_reason() {
  log "unloading gx-reason via llama-swap API"
  curl -fsS -m 15 -X POST -H "Authorization: Bearer ${GX_SWAP_API_KEY}" \
    "${GX_SWAP_BASE}/api/models/unload/gx-reason" >/dev/null 2>&1 || true
}

wait_for_unload() {
  local i
  for i in $(seq 1 "${UNLOAD_WAIT_TRIES}"); do
    if require_mem_free; then
      log "memory reclaimed after unload (attempt ${i}/${UNLOAD_WAIT_TRIES})"
      return 0
    fi
    sleep 5
  done
  return 1
}

# ------------------------------------------------------------------ run: gpu
GPU_OUTPUT=""
GPU_VERDICT=""

run_gpu_path() {
  log "=== run 1: GPU path (managed gx-reason, --n-gpu-layers 99) ==="
  require_mem_free || die "insufficient free memory before the GPU run; aborting rather than risk a repeat of B-012"

  local body resp
  body=$(python3 -c '
import json, sys
print(json.dumps({
    "model": "gx-reason",
    "prompt": sys.argv[1],
    "max_tokens": int(sys.argv[2]),
    "temperature": 0,
}))
' "${PROMPT}" "${N_PREDICT}")

  log "sending diagnostic prompt to gx-reason via ${GX_SWAP_BASE}/v1/completions (this triggers llama-swap to load it; may take several minutes cold)"
  resp=$(curl -fsS -m "${GPU_LOAD_TIMEOUT}" -X POST \
    -H "Authorization: Bearer ${GX_SWAP_API_KEY}" -H 'Content-Type: application/json' \
    "${GX_SWAP_BASE}/v1/completions" -d "${body}" 2>&1)
  local rc=$?

  if [ "${rc}" -ne 0 ]; then
    log "WARN: /v1/completions failed (rc=${rc}); trying the native /completion endpoint via docker exec as a fallback (llama-swap image has curl -- see llama-swap/llama-swap.Dockerfile)"
    local native_body
    native_body=$(python3 -c '
import json, sys
print(json.dumps({"prompt": sys.argv[1], "n_predict": int(sys.argv[2]), "temperature": 0}))
' "${PROMPT}" "${N_PREDICT}")
    resp=$(remote_slow "${GPU_LOAD_TIMEOUT}" \
      "docker exec gx-llama-swap-node02 curl -fsS -m $((GPU_LOAD_TIMEOUT - 10)) -X POST http://127.0.0.1:25800/completion -H 'Content-Type: application/json' -d '${native_body}'" 2>&1)
    rc=$?
    [ "${rc}" -eq 0 ] || { log "FAIL: could not reach gx-reason via either endpoint: ${resp:0:300}"; return 1; }
    GPU_OUTPUT=$(printf '%s' "${resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))' 2>/dev/null)
  else
    GPU_OUTPUT=$(printf '%s' "${resp}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("choices", [{}])[0].get("text", ""))
' 2>/dev/null)
  fi

  log "GPU path raw output (first 200 chars): $(printf '%s' "${GPU_OUTPUT}" | head -c 200)"
  GPU_VERDICT=$(classify_output "${GPU_OUTPUT}")
  log "GPU path verdict: ${GPU_VERDICT}"
}

# ------------------------------------------------------------------ run: cpu
CPU_OUTPUT=""
CPU_VERDICT=""

run_cpu_path() {
  log "=== run 2: CPU-only path (standalone container, --n-gpu-layers 0, no GPU device attached) ==="
  require_mem_free || die "insufficient free memory before the CPU-only run; aborting rather than risk a repeat of B-012"

  log "starting standalone diagnostic container ${CPU_CONTAINER} on node2 loopback:${CPU_PORT} (never published to the fabric or LAN)"
  remote "docker rm -f ${CPU_CONTAINER} >/dev/null 2>&1 || true"
  remote "docker run --rm -d --name ${CPU_CONTAINER} \
    -p 127.0.0.1:${CPU_PORT}:${CPU_PORT} \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --security-opt seccomp=unconfined \
    -v ${MODELS_ROOT}/gguf:/models/gguf:ro \
    ${GGUF_IMAGE} \
    -m ${MODEL_FILE} \
    --alias gx-reason-diag-cpu \
    --host 0.0.0.0 --port ${CPU_PORT} \
    --ctx-size 32768 \
    --n-gpu-layers 0 \
    --parallel 1 \
    --load-mode mmap \
    --timeout 300" >/dev/null 2>&1
  if [ $? -ne 0 ]; then
    log "FAIL: could not start ${CPU_CONTAINER}"
    return 1
  fi
  CPU_CONTAINER_STARTED=1

  log "waiting for the CPU-only server to become healthy (budget ${CPU_LOAD_TIMEOUT}s -- a 77GB CPU-only mmap load can be slow on first touch)"
  local deadline=$(( $(date +%s) + CPU_LOAD_TIMEOUT ))
  local healthy=0
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if remote "curl -fsS -m 5 http://127.0.0.1:${CPU_PORT}/health >/dev/null 2>&1"; then
      healthy=1
      break
    fi
    if ! remote "docker inspect -f '{{.State.Running}}' ${CPU_CONTAINER} 2>/dev/null" | grep -q true; then
      log "FAIL: ${CPU_CONTAINER} exited during startup"
      remote "docker logs --tail 60 ${CPU_CONTAINER} 2>&1" || true
      return 1
    fi
    sleep 10
  done
  [ "${healthy}" -eq 1 ] || { log "FAIL: ${CPU_CONTAINER} did not become healthy within ${CPU_LOAD_TIMEOUT}s"; return 1; }
  log "CPU-only server healthy"

  local native_body resp
  native_body=$(python3 -c '
import json, sys
print(json.dumps({"prompt": sys.argv[1], "n_predict": int(sys.argv[2]), "temperature": 0}))
' "${PROMPT}" "${N_PREDICT}")

  resp=$(remote "curl -fsS -m 300 -X POST http://127.0.0.1:${CPU_PORT}/completion -H 'Content-Type: application/json' -d '${native_body}'" 2>&1)
  if [ $? -ne 0 ]; then
    log "FAIL: request to CPU-only server failed: ${resp:0:300}"
    return 1
  fi
  CPU_OUTPUT=$(printf '%s' "${resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))' 2>/dev/null)
  log "CPU path raw output (first 200 chars): $(printf '%s' "${CPU_OUTPUT}" | head -c 200)"
  CPU_VERDICT=$(classify_output "${CPU_OUTPUT}")
  log "CPU path verdict: ${CPU_VERDICT}"

  remote "docker stop -t 30 ${CPU_CONTAINER} >/dev/null 2>&1 || true"
  CPU_CONTAINER_STARTED=0
}

# --------------------------------------------------------------------- main
main() {
  preflight

  unload_gx_reason
  wait_for_unload || die "memory did not recover after unloading gx-reason within budget; refusing to start the GPU run on top of whatever is still resident"

  run_gpu_path || die "GPU-path run did not complete; see log above. Aborting before touching the CPU path."

  unload_gx_reason
  wait_for_unload || die "memory did not recover after the GPU run; refusing to start the CPU-only run while something may still be resident (this is exactly the guard B-012 needed)"

  run_cpu_path || die "CPU-path run did not complete; see log above."

  echo "=============================================="
  echo " gx-reason B-011 diagnostic result"
  echo "=============================================="
  printf ' prompt:      %s\n' "${PROMPT}"
  printf ' n_predict:   %s\n' "${N_PREDICT}"
  printf ' GPU  output: %s\n' "$(printf '%s' "${GPU_OUTPUT}" | head -c 200)"
  printf ' GPU  verdict: %s\n' "${GPU_VERDICT}"
  printf ' CPU  output: %s\n' "$(printf '%s' "${CPU_OUTPUT}" | head -c 200)"
  printf ' CPU  verdict: %s\n' "${CPU_VERDICT}"
  echo "----------------------------------------------"

  case "${GPU_VERDICT}" in
    SANE*)
      echo " DIAGNOSIS: GPU path produced SANE output. B-011 may already be"
      echo " resolved, or the earlier failure was transient/build-specific."
      echo " A human should re-verify against the original repro before"
      echo " closing BLOCKERS.md B-011."
      ;;
    *)
      if [[ "${GPU_VERDICT}" == GARBAGE* && "${CPU_VERDICT}" == SANE* ]]; then
        echo " DIAGNOSIS: GPU path is GARBAGE, CPU-only path is SANE, with"
        echo " identical weights and sampling params. This isolates the fault"
        echo " to the CUDA/kernel execution path for this llama.cpp build's"
        echo " qwen3_5_moe hybrid (GDN + full-attention) implementation."
        echo " -> BLOCKERS.md B-011 hypothesis 1. Next step: rebuild"
        echo "    legenex/llama-cpp-spark from current master, or search/file"
        echo "    an upstream llama.cpp issue for qwen3_5_moe CUDA kernels."
      elif [[ "${GPU_VERDICT}" == GARBAGE* && "${CPU_VERDICT}" == GARBAGE* ]]; then
        echo " DIAGNOSIS: BOTH paths are GARBAGE with identical weights and"
        echo " sampling params. The fault reproduces with --n-gpu-layers 0,"
        echo " so it is NOT specific to the CUDA/GPU execution path."
        echo " -> BLOCKERS.md B-011 hypothesis 2/3: a bad dynamic quant or a"
        echo "    stale llama.cpp build. Next step: try a different quant"
        echo "    (unsloth UD-IQ4_XS, or bartowski/Qwen_Qwen3.5-122B-A10B-GGUF)"
        echo "    or rebuild llama.cpp from current master."
      else
        echo " DIAGNOSIS: inconsistent result (GPU=${GPU_VERDICT%% *}, CPU=${CPU_VERDICT%% *})."
        echo " Do not draw an automatic conclusion -- re-run and inspect the"
        echo " full logs by hand."
      fi
      ;;
  esac
  echo "=============================================="
}

main
