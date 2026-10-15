#!/usr/bin/env bash
# ============================================================================
# start.sh — DeepSeek-V4-Flash-0731 (abliterated) on 2x GB10: vLLM TP=2, nnodes=2
# ============================================================================
#
#   head   gx10-01 (this host): rank 0 + OpenAI API on 127.0.0.1:${DSV4F_PORT}
#   worker gx10-02 (DSV4F_WORKER_SSH): rank 1, --headless
#   weights live on gx10-02; the head reads them over a read-only NFSv4
#   export bound to the fabric (nfs/entrypoint.sh)
#
# Usage:
#   ./start.sh [start]    preflight -> NFS -> worker -> head -> health -> floor
#   ./start.sh stop       sanctioned stop path: the two rank containers, the
#                         head's NFS volume, then the worker's exporter
#   ./start.sh status     containers + API health
#   ./start.sh preflight  every check of `start`, launches nothing
#   ./start.sh harden     owner-only permissions on the weights and caches
#
# Never: --trust-remote-code, a 0.0.0.0 API bind, sudo, a GID pin.
# Output lines starting with "=== " are phase markers the orchestrator reads.
# ============================================================================
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------- config
# Caller-wins loading: a DSV4F_* key already in the environment is kept; the
# canonical file and the optional local .env only fill what is unset.
load_env_file() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" =~ ^DSV4F_[A-Z0-9_]+$ ]] || continue
        value="${value%%[[:space:]]#*}"
        value="${value%\"}"; value="${value#\"}"
        if [ -z "${!key+x}" ]; then
            printf -v "$key" '%s' "$value"
            export "${key?}"
        fi
    done < "$file"
}
# Local overrides first so they beat the canonical defaults (caller beats both).
load_env_file "$KIT/.env"
load_env_file "$KIT/dsv4-flash.env.canonical"

log()  { printf '[dsv4-flash] %s\n' "$*"; }
warn() { printf '[dsv4-flash] WARN: %s\n' "$*" >&2; }
die()  { printf '[dsv4-flash] ERROR: %s\n' "$*" >&2; exit 1; }

worker_ssh() { ssh -T -o BatchMode=yes -o ConnectTimeout=15 "$DSV4F_WORKER_SSH" "$@"; }

HEAD_CACHE="$HOME/$DSV4F_CACHE_DIRNAME"
WORKER_CACHE="$DSV4F_WORKER_HOME/$DSV4F_CACHE_DIRNAME"
STATE_DIR="$HEAD_CACHE/run"
SVC_UID="$(id -u)"
SVC_GID="$(id -g)"

seqs="$DSV4F_MAX_NUM_SEQS"
k="$DSV4F_DSPARK_K"
mnbt="${DSV4F_MAX_NUM_BATCHED_TOKENS:-}"
[ -n "$mnbt" ] || mnbt=$(( 8192 + seqs * (k + 1) ))
capture=$(( seqs * (k + 1) ))

# ------------------------------------------------------------------ validate
validate_config() {
    local v
    for v in DSV4F_MAX_MODEL_LEN DSV4F_MAX_NUM_SEQS DSV4F_DSPARK_K DSV4F_PORT DSV4F_MASTER_PORT \
             DSV4F_TP DSV4F_NNODES DSV4F_BLOCK_SIZE DSV4F_READY_TIMEOUT DSV4F_GID_PREFERRED; do
        [[ "${!v}" =~ ^[0-9]+$ ]] || die "$v must be an integer, got '${!v}'"
    done
    [[ "$mnbt" =~ ^[0-9]+$ ]] || die "DSV4F_MAX_NUM_BATCHED_TOKENS must be an integer"
    [ "$k" -ge 1 ] && [ "$k" -le 5 ] || die "DSV4F_DSPARK_K=$k: DSpark needs 1..5 (dspark_block_size)"
    [ "$mnbt" -ge $(( 8192 + seqs * (k - 1) )) ] \
        || die "max-num-batched-tokens $mnbt < 8192 + max_num_seqs*(k-1) = $(( 8192 + seqs * (k - 1) ))"
    [ "$DSV4F_TP" = 2 ] && [ "$DSV4F_NNODES" = 2 ] || die "this kit is TP=2 over 2 nodes"
    [ "$DSV4F_API_HOST" = "127.0.0.1" ] || die "DSV4F_API_HOST must be 127.0.0.1 (the orchestrator proxies the API)"
    for v in DSV4F_HEAD_IP DSV4F_WORKER_IP; do
        [[ "${!v}" =~ ^192\.168\.10[01]\.[0-9]{1,3}$ ]] || die "$v=${!v} is not a fabric address (L-3)"
    done
    [ "$DSV4F_MOE_BACKEND" = flashinfer_b12x ] || die "--moe-backend must be flashinfer_b12x on GB10 (auto = slow DEEPGEMM path)"
    awk -v u="$DSV4F_GPU_MEM_UTIL" 'BEGIN { exit !(u > 0.3 && u < 0.95) }' || die "DSV4F_GPU_MEM_UTIL=$DSV4F_GPU_MEM_UTIL out of range"
    return 0
}

# ------------------------------------------------------------------ GID
# Echo the index of the RoCE v2 GID carrying $ip on $ib (sysfs; read-only).
resolve_gid_local() {
    local ib="$1" ip="$2" pref="$3" tail i g t found=""
    tail=$(printf '%02x%02x:%02x%02x' ${ip//./ })
    local base="/sys/class/infiniband/$ib/ports/1"
    for i in "$pref" $(seq 0 15); do
        g=$(cat "$base/gids/$i" 2>/dev/null) || continue
        t=$(cat "$base/gid_attrs/types/$i" 2>/dev/null) || continue
        if [ "$t" = "RoCE v2" ] && [[ "$g" == *":ffff:$tail" ]]; then found="$i"; break; fi
    done
    printf '%s' "$found"
}
resolve_gid_worker() {
    local ib="$1" ip="$2" pref="$3"
    worker_ssh "ib='$ib' ip='$ip' pref='$pref' bash -s" <<'EOF'
tail=$(printf '%02x%02x:%02x%02x' ${ip//./ })
base="/sys/class/infiniband/$ib/ports/1"
for i in "$pref" $(seq 0 15); do
  g=$(cat "$base/gids/$i" 2>/dev/null) || continue
  t=$(cat "$base/gid_attrs/types/$i" 2>/dev/null) || continue
  if [ "$t" = "RoCE v2" ] && [[ "$g" == *":ffff:$tail" ]]; then printf '%s' "$i"; exit 0; fi
done
EOF
}

# ------------------------------------------------------------------ preflight
image_id_local()  { docker image inspect -f '{{.Id}}' "$DSV4F_IMAGE" 2>/dev/null || true; }
image_id_worker() { worker_ssh "docker image inspect -f '{{.Id}}' '$DSV4F_IMAGE' 2>/dev/null" || true; }

check_perms_worker() {
    # The weights must not be writable by anyone but their owner, and must be
    # owned by the service user (all_squash maps NFS reads to that uid).
    worker_ssh "d='$DSV4F_WORKER_MODEL_DIR'; uid=\$(id -u);
        [ -d \"\$d\" ] || { echo 'missing model dir'; exit 3; }
        [ \"\$(stat -c %u \"\$d\")\" = \"\$uid\" ] || { echo 'model dir not owned by the service user'; exit 4; }
        bad=\$(find \"\$d\" \\( -perm -0002 -o -perm -0020 \\) -print -quit)
        [ -z \"\$bad\" ] || { echo \"group/world-writable: \$bad (run ./start.sh harden)\"; exit 5; }
        exit 0"
}

preflight() {
    echo "=== gx-max preflight ==="
    validate_config
    command -v docker >/dev/null || die "docker missing on head"
    worker_ssh true || die "cannot ssh to the worker ($DSV4F_WORKER_SSH)"
    local kh kw
    kh="$(uname -r)"; kw="$(worker_ssh uname -r)"
    [ "$kh" = "6.17.0-1032-nvidia" ] && [ "$kw" = "6.17.0-1032-nvidia" ] || die "kernel must be 6.17.0-1032-nvidia on both nodes (L-4): head=$kh worker=$kw"

    # Image present, same id on both nodes, and the pinned id if recorded.
    local ih iw
    ih="$(image_id_local)"; iw="$(image_id_worker)"
    [ -n "$iw" ] || die "image $DSV4F_IMAGE missing on the worker (build-image.sh on gx10-02)"
    [ -n "$ih" ] || die "image $DSV4F_IMAGE missing on the head (docker save | ssh docker load; needs ~disk, see README)"
    [ "$ih" = "$iw" ] || die "image id differs: head $ih worker $iw"
    if [ -n "${DSV4F_IMAGE_ID:-}" ] && [ "$ih" != "$DSV4F_IMAGE_ID" ]; then
        die "image id $ih is not the pinned DSV4F_IMAGE_ID $DSV4F_IMAGE_ID"
    fi
    worker_ssh "docker image inspect '$DSV4F_NFS_IMAGE' >/dev/null 2>&1" || {
        log "shipping $DSV4F_NFS_IMAGE to the worker (small alpine nfs-utils image)"
        docker image inspect "$DSV4F_NFS_IMAGE" >/dev/null 2>&1 || die "$DSV4F_NFS_IMAGE not on the head either"
        docker save "$DSV4F_NFS_IMAGE" | worker_ssh docker load >/dev/null
    }

    # Weights: verified download, owner-only permissions, overlay proof.
    worker_ssh "test -f '$DSV4F_WORKER_MODEL_DIR/VERIFY.json'" || die "weights not verified yet (no VERIFY.json)"
    worker_ssh "python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d[\"files\"]==d[\"verified\"]==60 else 1)' '$DSV4F_WORKER_MODEL_DIR/VERIFY.json'" \
        || die "VERIFY.json does not show 60/60 verified files"
    check_perms_worker || die "weight permissions refused (see above)"
    sync_kit_to_worker
    worker_ssh "python3 -I '$WORKER_CACHE/kit/tools/overlay_check.py' verify '$DSV4F_WORKER_MODEL_DIR'" > "$STATE_DIR/overlay-verify.json" \
        || { cat "$STATE_DIR/overlay-verify.json" >&2; die "overlay loader-order proof failed"; }
    log "overlay order proof OK ($(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["overlay_file"], "read last;", d["overlay_tensors"], "tensors")' "$STATE_DIR/overlay-verify.json"))"

    # Fabric: populated RoCE v2 GID for each rank's fabric IP.
    [ -n "${DSV4F_HEAD_GID:-}" ] || DSV4F_HEAD_GID="$(resolve_gid_local "$DSV4F_HEAD_IB" "$DSV4F_HEAD_IP" "$DSV4F_GID_PREFERRED")"
    [ -n "${DSV4F_WORKER_GID:-}" ] || DSV4F_WORKER_GID="$(resolve_gid_worker "$DSV4F_WORKER_IB" "$DSV4F_WORKER_IP" "$DSV4F_GID_PREFERRED")"
    [ -n "$DSV4F_HEAD_GID" ] || die "no RoCE v2 GID for $DSV4F_HEAD_IP on head $DSV4F_HEAD_IB (fabric down?)"
    [ -n "$DSV4F_WORKER_GID" ] || die "no RoCE v2 GID for $DSV4F_WORKER_IP on worker $DSV4F_WORKER_IB (fabric down?)"
    log "NCCL GID index head=$DSV4F_HEAD_GID worker=$DSV4F_WORKER_GID (hca $DSV4F_HEAD_IB / $DSV4F_WORKER_IB)"

    # Nothing of ours or of the V4.1 kit may be running; ports free.
    docker ps --format '{{.Names}}' | grep -Eq '^(dsv41-exl3-head|'"$DSV4F_CONTAINER_HEAD"')$' \
        && die "a model head container is already running on gx10-01"
    worker_ssh "docker ps --format '{{.Names}}'" | grep -Eq '^(dsv41-exl3-worker|'"$DSV4F_CONTAINER_WORKER"')$' \
        && die "a model worker container is already running on gx10-02"
    local p
    for p in "$DSV4F_PORT" "$DSV4F_MASTER_PORT"; do
        ss -Htln "sport = :$p" 2>/dev/null | grep -q . && die "port $p already in use on the head"
    done

    # Unified memory: each node must keep the resident floor after load.
    local mh mw
    mh="$(python3 -I "$KIT/tools/mem_budget.py" from-meminfo --util "$DSV4F_GPU_MEM_UTIL" \
          --floor-gib "$DSV4F_RESIDENT_FLOOR_GIB" --overhead-gib "$DSV4F_MEM_OVERHEAD_GIB")" \
        || die "head memory refused: $mh"
    mw="$(worker_ssh "python3 -I '$WORKER_CACHE/kit/tools/mem_budget.py' from-meminfo --util '$DSV4F_GPU_MEM_UTIL' --floor-gib '$DSV4F_RESIDENT_FLOOR_GIB' --overhead-gib '$DSV4F_MEM_OVERHEAD_GIB'")" \
        || die "worker memory refused: $mw"
    log "memory head: $mh"
    log "memory worker: $mw"
}

sync_kit_to_worker() {
    worker_ssh "umask 077; mkdir -p '$WORKER_CACHE/kit/tools' '$WORKER_CACHE/kit/nfs' && chmod 700 '$WORKER_CACHE'"
    scp -q -o BatchMode=yes "$KIT/tools/overlay_check.py" "$KIT/tools/mem_budget.py" "$DSV4F_WORKER_SSH:$WORKER_CACHE/kit/tools/"
    scp -q -o BatchMode=yes "$KIT/nfs/entrypoint.sh" "$DSV4F_WORKER_SSH:$WORKER_CACHE/kit/nfs/entrypoint.sh"
}

# ------------------------------------------------------------------ caches
prepare_caches() {
    umask 077
    mkdir -p "$HEAD_CACHE"/{home,hf,vllm,triton,flashinfer,tmp} "$STATE_DIR"
    chmod 700 "$HEAD_CACHE" "$HEAD_CACHE"/*
    worker_ssh "umask 077; mkdir -p '$WORKER_CACHE'/{home,hf,vllm,triton,flashinfer,tmp,run} && chmod 700 '$WORKER_CACHE' '$WORKER_CACHE'/*"
}

# ------------------------------------------------------------------ NFS
nfs_up() {
    log "NFS: exporting the weights from the worker (fabric $DSV4F_WORKER_IP -> client $DSV4F_HEAD_IP only)"
    worker_ssh "docker rm -f '$DSV4F_NFS_CONTAINER' >/dev/null 2>&1 || true
        docker run -d --name '$DSV4F_NFS_CONTAINER' --restart no --privileged --network host \
          -v '$DSV4F_WORKER_MODEL_DIR:/export:ro' \
          -v '$WORKER_CACHE/kit/nfs/entrypoint.sh:/gx-nfs-entrypoint.sh:ro' \
          -e NFS_BIND_IP='$DSV4F_WORKER_IP' -e NFS_CLIENT_IP='$DSV4F_HEAD_IP' \
          -e NFS_ANON_UID=\$(id -u) -e NFS_ANON_GID=\$(id -g) \
          --entrypoint /bin/bash '$DSV4F_NFS_IMAGE' /gx-nfs-entrypoint.sh >/dev/null"
    local i
    for i in $(seq 1 30); do
        if timeout 3 bash -c "</dev/tcp/$DSV4F_WORKER_IP/2049" 2>/dev/null; then break; fi
        sleep 1
        [ "$i" = 30 ] && { worker_ssh "docker logs '$DSV4F_NFS_CONTAINER'" >&2 || true; die "NFS server did not listen on $DSV4F_WORKER_IP:2049"; }
    done
    docker volume rm "$DSV4F_HEAD_NFS_VOLUME" >/dev/null 2>&1 || true
    docker volume create --driver local --opt type=nfs \
        --opt "o=addr=$DSV4F_WORKER_IP,$DSV4F_NFS_CLIENT_OPTS" \
        --opt "device=:/" "$DSV4F_HEAD_NFS_VOLUME" >/dev/null
    docker run --rm --network none -v "$DSV4F_HEAD_NFS_VOLUME:/m:ro" --entrypoint test "$DSV4F_IMAGE" -f /m/config.json \
        || die "the head cannot read the weights over NFS (volume $DSV4F_HEAD_NFS_VOLUME)"
    log "NFS: head sees the weights"
}

nfs_down() {
    docker volume rm "$DSV4F_HEAD_NFS_VOLUME" >/dev/null 2>&1 || true
    worker_ssh "if docker ps -a --format '{{.Names}}' | grep -qx '$DSV4F_NFS_CONTAINER'; then
        docker stop -t 20 '$DSV4F_NFS_CONTAINER' >/dev/null 2>&1 || true
        docker rm -f '$DSV4F_NFS_CONTAINER' >/dev/null 2>&1 || true; fi" || warn "could not stop the worker NFS exporter"
}

# ------------------------------------------------------------------ launch
write_env_file() {
    # $1 = output path, $2 = rank (0|1)
    local out="$1" rank="$2" ifname ib gid hostip
    if [ "$rank" = 0 ]; then ifname="$DSV4F_HEAD_IF"; ib="$DSV4F_HEAD_IB"; gid="$DSV4F_HEAD_GID"; hostip="$DSV4F_HEAD_IP"
    else ifname="$DSV4F_WORKER_IF"; ib="$DSV4F_WORKER_IB"; gid="$DSV4F_WORKER_GID"; hostip="$DSV4F_WORKER_IP"; fi
    umask 077
    cat > "$out" <<EOF
GX_RANK=$rank
GX_SERVED_MODEL_NAME=$DSV4F_SERVED_MODEL_NAME
GX_API_HOST=$DSV4F_API_HOST
GX_PORT=$DSV4F_PORT
GX_MASTER_ADDR=$DSV4F_HEAD_IP
GX_MASTER_PORT=$DSV4F_MASTER_PORT
GX_MODEL=$DSV4F_MODEL_MOUNT
GX_MAX_MODEL_LEN=$DSV4F_MAX_MODEL_LEN
GX_MAX_NUM_SEQS=$seqs
GX_MAX_NUM_BATCHED_TOKENS=$mnbt
GX_MAX_CUDAGRAPH_CAPTURE=$capture
GX_DSPARK_K=$k
GX_GPU_MEM_UTIL=$DSV4F_GPU_MEM_UTIL
GX_KV_CACHE_DTYPE=$DSV4F_KV_CACHE_DTYPE
GX_BLOCK_SIZE=$DSV4F_BLOCK_SIZE
GX_MOE_BACKEND=$DSV4F_MOE_BACKEND
GX_PREFIX_CACHING=$DSV4F_ENABLE_PREFIX_CACHING
GX_FLASHINFER_AUTOTUNE=$DSV4F_FLASHINFER_AUTOTUNE
HOME=/cache/home
USER=gx
LOGNAME=gx
HF_HOME=/cache/hf
VLLM_CACHE_ROOT=/cache/vllm
TRITON_CACHE_DIR=/cache/triton
FLASHINFER_WORKSPACE_BASE=/cache/flashinfer
TMPDIR=/cache/tmp
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
HF_HUB_DISABLE_TELEMETRY=1
VLLM_NO_USAGE_STATS=1
DO_NOT_TRACK=1
GX_WOB_DIGEST=$DSV4F_WOB_DIGEST
VLLM_HOST_IP=$hostip
NCCL_SOCKET_IFNAME=$ifname
GLOO_SOCKET_IFNAME=$ifname
TP_SOCKET_IFNAME=$ifname
NCCL_IB_HCA=$ib
NCCL_IB_GID_INDEX=$gid
NCCL_IB_DISABLE=0
NCCL_NET=IB
NCCL_NET_PLUGIN=none
NCCL_IB_ROCE_VERSION_NUM=2
NCCL_IB_ADDR_FAMILY=AF_INET
NCCL_CROSS_NIC=0
NCCL_CUMEM_ENABLE=0
NCCL_NVLS_ENABLE=0
NCCL_IGNORE_CPU_AFFINITY=1
NCCL_DEBUG=$DSV4F_NCCL_DEBUG
VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
VLLM_TRITON_MLA_SPARSE=1
VLLM_SPARSE_INDEXER_MAX_LOGITS_MB=256
VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0
VLLM_USE_FLASHINFER_SAMPLER=1
VLLM_USE_B12X_MOE=1
VLLM_USE_B12X_WO_PROJECTION=1
VLLM_B12X_W4A16_FORCE_BLOCKS_PER_SM=0
VLLM_B12X_W4A16_FORCE_BLOCKS_MAX_M=16
B12X_W4A16_TC_DECODE=0
VLLM_DSPARK_CONFIDENCE_THRESHOLD=0.0
VLLM_DSPARK_CONFIDENCE_SCHEDULER=off
VLLM_DSPARK_LOCAL_ARGMAX=1
VLLM_DSPARK_REPLICATE_MARKOV_W1=1
VLLM_DSPARK_FUSED_MARKOV_ARGMAX=0
VLLM_DSPARK_GPU_REJECTED_CONTEXT_MASK=1
VLLM_DSPARK_REFERENCE_KV_QUANT_DEQUANT=0
VLLM_DSPARK_HARDWARE_SCHEDULER_EARLY_STOP=1
VLLM_DSV4_B12X_COMPRESSED_MLA=0
DSPARK_SLOT_CLAMP=1
TORCH_CUDA_ARCH_LIST=12.1a
FLASHINFER_CUDA_ARCH_LIST=12.1a
FLASHINFER_DISABLE_VERSION_CHECK=1
TILELANG_CLEANUP_TEMP_FILES=1
DG_JIT_USE_NVRTC=0
DG_JIT_NVCC_COMPILER=/usr/local/cuda/bin/nvcc
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
EOF
}

write_inner_script() {
    umask 077
    cat > "$1" <<'EOF'
#!/bin/bash
# In-container launcher (generated by start.sh; mounted read-only).
set -euo pipefail
export PATH="/usr/local/cuda/bin:${PATH:-/usr/bin:/bin}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}" CUDA_PATH="${CUDA_HOME:-/usr/local/cuda}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
mkdir -p "$HOME" "$TMPDIR"
SPEC="$(python3 -S -c 'import json,os; print(json.dumps({"method":"dspark","num_speculative_tokens":int(os.environ["GX_DSPARK_K"]),"draft_sample_method":"probabilistic"},separators=(",",":")))')"
ARGS=(
  --served-model-name "$GX_SERVED_MODEL_NAME"
  --host "$GX_API_HOST" --port "$GX_PORT"
  --tensor-parallel-size 2 --pipeline-parallel-size 1
  --distributed-executor-backend mp
  --nnodes 2 --node-rank "$GX_RANK"
  --master-addr "$GX_MASTER_ADDR" --master-port "$GX_MASTER_PORT"
  --load-format safetensors
  --kv-cache-dtype "$GX_KV_CACHE_DTYPE" --block-size "$GX_BLOCK_SIZE"
  --max-model-len "$GX_MAX_MODEL_LEN"
  --max-num-seqs "$GX_MAX_NUM_SEQS"
  --max-num-batched-tokens "$GX_MAX_NUM_BATCHED_TOKENS"
  --max-cudagraph-capture-size "$GX_MAX_CUDAGRAPH_CAPTURE"
  --gpu-memory-utilization "$GX_GPU_MEM_UTIL"
  --async-scheduling --enable-chunked-prefill
  --speculative-config "$SPEC"
  --tokenizer-mode deepseek_v4
  --moe-backend "$GX_MOE_BACKEND"
  --tool-call-parser deepseek_v4 --enable-auto-tool-choice
  --reasoning-parser deepseek_v4
  --reasoning-config '{"reasoning_parser":"deepseek_v4","reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
  --default-chat-template-kwargs '{"thinking":false}'
  --generation-config vllm
  --enable-prompt-tokens-details
  --jit-monitor-mode warn
)
[ "$GX_PREFIX_CACHING" = 1 ] && ARGS+=(--enable-prefix-caching) || ARGS+=(--no-enable-prefix-caching)
[ "$GX_FLASHINFER_AUTOTUNE" = 1 ] && ARGS+=(--enable-flashinfer-autotune) || ARGS+=(--no-enable-flashinfer-autotune)
[ "$GX_RANK" = 1 ] && ARGS+=(--headless)
echo "[dsv4f-rank$GX_RANK] MemAvailable=$(awk '/^MemAvailable:/{printf "%.1f", $2/1048576}' /proc/meminfo) GiB uid=$(id -u)"
echo "[dsv4f-rank$GX_RANK] vllm serve $GX_MODEL ${ARGS[*]}"
exec vllm serve "$GX_MODEL" "${ARGS[@]}"
EOF
    chmod 500 "$1"
}

run_flags() {
    # Shared docker-run flags; $1 = cache root on that node.
    local cache="$1" user_flag=""
    # As the service user every capability can go; the root fallback keeps the
    # defaults (it needs DAC override to use the 0700 caches).
    [ "$DSV4F_RUN_AS_SERVICE_USER" = 1 ] && user_flag="--user $SVC_UID:$SVC_GID --cap-drop ALL"
    printf '%s' "--network host --ipc host --shm-size $DSV4F_SHM_SIZE --stop-timeout 60 \
 --device nvidia.com/gpu=all --device /dev/infiniband \
 --ulimit memlock=-1 --ulimit stack=67108864 --oom-score-adj 900 \
 --security-opt no-new-privileges $user_flag \
 -v $cache/home:/cache/home -v $cache/hf:/cache/hf -v $cache/vllm:/cache/vllm \
 -v $cache/triton:/cache/triton -v $cache/flashinfer:/cache/flashinfer -v $cache/tmp:/cache/tmp \
 -v $cache/run/inner.sh:/cache/inner.sh:ro --env-file $cache/run/rank.env"
}

launch() {
    echo "=== starting the dsv4-flash kit (worker rank 1, then head rank 0) ==="
    write_inner_script "$STATE_DIR/inner.sh"
    # Security review condition 5: the model has no auto_map; remote code is never trusted.
    grep -q -- '--trust-remote-code' "$STATE_DIR/inner.sh" && die "--trust-remote-code is forbidden"
    write_env_file "$STATE_DIR/rank0.env" 0
    write_env_file "$STATE_DIR/rank1.env" 1
    cp "$STATE_DIR/rank0.env" "$STATE_DIR/rank.env"
    worker_ssh "umask 077; mkdir -p '$WORKER_CACHE/run'"
    scp -q -o BatchMode=yes "$STATE_DIR/inner.sh" "$DSV4F_WORKER_SSH:$WORKER_CACHE/run/inner.sh"
    scp -q -o BatchMode=yes "$STATE_DIR/rank1.env" "$DSV4F_WORKER_SSH:$WORKER_CACHE/run/rank.env"

    log "worker: $DSV4F_CONTAINER_WORKER on $DSV4F_WORKER_SSH (local weights)"
    # shellcheck disable=SC2046
    worker_ssh "docker rm -f '$DSV4F_CONTAINER_WORKER' >/dev/null 2>&1 || true
        docker run -d --name '$DSV4F_CONTAINER_WORKER' $(run_flags "$WORKER_CACHE") \
          -v '$DSV4F_WORKER_MODEL_DIR:$DSV4F_MODEL_MOUNT:ro' \
          --entrypoint bash '$DSV4F_IMAGE' /cache/inner.sh >/dev/null"
    sleep 5
    log "head: $DSV4F_CONTAINER_HEAD (weights over NFS volume $DSV4F_HEAD_NFS_VOLUME)"
    docker rm -f "$DSV4F_CONTAINER_HEAD" >/dev/null 2>&1 || true
    # shellcheck disable=SC2046
    eval docker run -d --name "$DSV4F_CONTAINER_HEAD" $(run_flags "$HEAD_CACHE") \
        -v "$DSV4F_HEAD_NFS_VOLUME:$DSV4F_MODEL_MOUNT:ro" \
        --entrypoint bash "$DSV4F_IMAGE" /cache/inner.sh >/dev/null
}

container_running_head()   { docker inspect -f '{{.State.Running}}' "$DSV4F_CONTAINER_HEAD" 2>/dev/null | grep -q true; }
container_running_worker() { worker_ssh "docker inspect -f '{{.State.Running}}' '$DSV4F_CONTAINER_WORKER' 2>/dev/null" | grep -q true; }

wait_health() {
    echo "=== waiting for the model to become healthy (timeout ${DSV4F_READY_TIMEOUT}s) ==="
    local url="http://127.0.0.1:$DSV4F_PORT/health" t=0 wfail=0 last=0 quiet=0 cur
    while [ "$t" -lt "$DSV4F_READY_TIMEOUT" ]; do
        if curl -fsS -m 5 "$url" >/dev/null 2>&1; then return 0; fi
        container_running_head || { warn "head container exited"; docker logs --tail 60 "$DSV4F_CONTAINER_HEAD" >&2 || true; return 1; }
        if container_running_worker; then wfail=0; else
            wfail=$((wfail + 1))
            [ "$wfail" -ge 3 ] && { warn "worker container not running"; worker_ssh "docker logs --tail 60 '$DSV4F_CONTAINER_WORKER'" >&2 || true; return 1; }
        fi
        cur=$(docker logs "$DSV4F_CONTAINER_HEAD" 2>&1 | wc -l)
        if [ "$cur" -eq "$last" ]; then quiet=$((quiet + 10)); else quiet=0; last=$cur; fi
        [ "$quiet" -ge "$DSV4F_HANG_SECONDS" ] && { warn "no head log line for ${quiet}s: treating as a hang"; return 1; }
        [ $((t % 60)) -eq 0 ] && log "t+${t}s MemAvailable head=$(awk '/^MemAvailable:/{printf "%.1f",$2/1048576}' /proc/meminfo) GiB"
        sleep 10; t=$((t + 10))
    done
    warn "timed out after ${t}s"
    return 1
}

memory_floor_after_load() {
    local h w floor="$DSV4F_RESIDENT_FLOOR_GIB"
    h=$(awk '/^MemAvailable:/{printf "%.1f",$2/1048576}' /proc/meminfo)
    w=$(worker_ssh "awk '/^MemAvailable:/{printf \"%.1f\",\$2/1048576}' /proc/meminfo")
    log "MemAvailable after load: head=${h} GiB worker=${w} GiB (floor ${floor} GiB)"
    if ! awk -v a="$h" -v b="$w" -v f="$floor" 'BEGIN { exit !(a + 0 >= f && b + 0 >= f) }'; then
        if [ "$DSV4F_FLOOR_ENFORCE" = 1 ]; then
            warn "a node is below the ${floor} GiB resident floor after load"
            return 1
        fi
        warn "below the resident floor (DSV4F_FLOOR_ENFORCE=0: continuing)"
    fi
}

digest_summary() {
    [ "$DSV4F_WOB_DIGEST" = 1 ] || return 0
    docker logs "$DSV4F_CONTAINER_HEAD" 2>&1 | grep 'GX_WOB_DIGEST' > "$STATE_DIR/wob-head.log" || true
    worker_ssh "docker logs '$DSV4F_CONTAINER_WORKER' 2>&1 | grep GX_WOB_DIGEST" > "$STATE_DIR/wob-worker.log" || true
    log "wo_b digests: head $(wc -l < "$STATE_DIR/wob-head.log") line(s), worker $(wc -l < "$STATE_DIR/wob-worker.log") line(s); compare with: tools/overlay_check.py compare <model_dir> $STATE_DIR/wob-*.log (on gx10-02)"
}

start() {
    local t0=$SECONDS
    prepare_caches
    preflight
    nfs_up
    launch
    if wait_health && memory_floor_after_load; then
        digest_summary
        echo "=== gx-max READY (dsv4-flash, $DSV4F_SERVED_MODEL_NAME on 127.0.0.1:$DSV4F_PORT) after $((SECONDS - t0))s ==="
        return 0
    fi
    warn "start failed; collecting logs and running the stop path"
    docker logs "$DSV4F_CONTAINER_HEAD" > "$STATE_DIR/head-fail.log" 2>&1 || true
    worker_ssh "docker logs '$DSV4F_CONTAINER_WORKER' 2>&1" > "$STATE_DIR/worker-fail.log" 2>&1 || true
    stop
    die "dsv4-flash did not become healthy (logs: $STATE_DIR/{head,worker}-fail.log)"
}

stop() {
    log "stopping head rank ($DSV4F_CONTAINER_HEAD)"
    docker stop -t 60 "$DSV4F_CONTAINER_HEAD" >/dev/null 2>&1 || true
    docker rm -f "$DSV4F_CONTAINER_HEAD" >/dev/null 2>&1 || true
    log "stopping worker rank ($DSV4F_CONTAINER_WORKER)"
    worker_ssh "docker stop -t 60 '$DSV4F_CONTAINER_WORKER' >/dev/null 2>&1 || true; docker rm -f '$DSV4F_CONTAINER_WORKER' >/dev/null 2>&1 || true" \
        || warn "worker unreachable while stopping"
    nfs_down
    log "stopped."
}

status() {
    docker ps -a --filter "name=^${DSV4F_CONTAINER_HEAD}\$" --format 'head   {{.Names}} {{.Status}}'
    worker_ssh "docker ps -a --filter 'name=^${DSV4F_CONTAINER_WORKER}\$' --format 'worker {{.Names}} {{.Status}}'; docker ps --filter 'name=^${DSV4F_NFS_CONTAINER}\$' --format 'nfs    {{.Names}} {{.Status}}'" || true
    if curl -fsS -m 5 "http://127.0.0.1:$DSV4F_PORT/health" >/dev/null 2>&1; then log "API healthy on 127.0.0.1:$DSV4F_PORT"; else log "API not responding"; fi
}

harden() {
    log "owner-only permissions: weights dirs 0750 / files 0640, caches 0700 (no sudo needed: the service user owns them)"
    worker_ssh "find '$DSV4F_WORKER_MODEL_DIR' -type d -exec chmod 0750 {} + && find '$DSV4F_WORKER_MODEL_DIR' -type f -exec chmod 0640 {} + && chmod 0750 '$(dirname "$DSV4F_WORKER_MODEL_DIR")'"
    prepare_caches
}

main() {
    case "${1:-start}" in
        start)            start ;;
        stop|--force)     stop ;;
        restart)          stop; start ;;
        status)           status ;;
        preflight)        prepare_caches; preflight; log "preflight OK" ;;
        harden)           harden ;;
        config)           validate_config; env | grep '^DSV4F_' | sort; echo "mnbt=$mnbt capture=$capture" ;;
        *) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 1 ;;
    esac
}

main "$@"
