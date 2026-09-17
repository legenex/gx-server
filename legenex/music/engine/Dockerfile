# =============================================================================
# gx-music engine -- ACE-Step 1.5 (XL) for NVIDIA GB10 (aarch64, CUDA 13).
#
# ENGINE ONLY. The upstream REST server (acestep.api_server) exposes
# /v1/audio?path=..., training routes and model-switching routes. It is bound
# to loopback inside the host network namespace and fronted by the gx-music
# supervisor (gx_music/), exactly like ComfyUI is fronted by gx-media-router.
#
# GPU ACCESS IS CDI:  docker run --device nvidia.com/gpu=all ...
# (--gpus all / --runtime nvidia do not exist on these nodes.)
#
# Same base tag as gx-comfyui:sm121 so the CUDA/Ubuntu layers are shared on
# disk. Upstream's own pyproject pins torch 2.10.0+cu130 for
# linux/aarch64 ("e.g. NVIDIA DGX Spark"); we install from its frozen uv.lock.
# =============================================================================
ARG CUDA_TAG=13.0.1-runtime-ubuntu24.04
FROM nvidia/cuda:${CUDA_TAG}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# python3-dev + build-essential: Triton JIT needs Python.h/gcc (see media Dockerfile).
# ffmpeg: mp3/opus/aac encode and decoding of uploaded reference audio.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv \
        build-essential git ca-certificates curl \
        ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# Pinned upstream commit (verified on gx10-02 2026-09-17).
ARG ACESTEP_REF=ca1e85fe9430179831e6bc6be790c332190a3866
RUN git clone https://github.com/ace-step/ACE-Step-1.5.git /app \
    && git -C /app checkout --detach ${ACESTEP_REF} \
    && git -C /app rev-parse HEAD > /app/.gx-acestep-ref \
    && rm -rf /app/.git

# GB10 TORCH OVERRIDE (measured 2026-09-17, evidence/torch214-probe.txt):
# upstream's lock pins torch 2.10.0+cu130, whose cuBLAS path fails EVERY gemm
# on GB10 (sm_121) with CUBLAS_STATUS_INVALID_VALUE -- even fp32. The set
# proven on this hardware by gx-comfyui is torch 2.14.0 / torchaudio 2.11.0 /
# torchvision 0.29.0 (cu130). Override it in the SAME layer so the 2.10 wheels
# never land in the image (node 2's disk is ~98% full).
ARG TORCH_SPEC="torch==2.14.0+cu130 torchaudio==2.11.0+cu130 torchvision==0.29.0+cu130"
WORKDIR /app
RUN uv sync --frozen --no-dev --python /usr/bin/python3.12 \
    && uv pip install --python /app/.venv/bin/python --index-strategy unsafe-best-match \
         --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple \
         ${TORCH_SPEC} \
    && rm -rf /root/.cache

ENV PATH=/app/.venv/bin:$PATH

# Fail the BUILD if the stack is wrong. (get_arch_list() is [] without a GPU,
# so the Blackwell cubin check runs at deploy time: scripts/engine-selftest.sh.)
RUN python -c "import torch, torchaudio; \
print('torch', torch.__version__, 'ta', torchaudio.__version__, 'cuda', torch.version.cuda); \
assert torch.version.cuda and torch.version.cuda.startswith('13'), 'expected CUDA 13'; \
assert torch.__version__.startswith('2.14'), 'GB10 torch override missing'" \
 && python -c "import acestep.api_server, acestep.handler, acestep.llm_inference; print('acestep import ok')"

COPY gx_engine_entry.py /app/gx_engine_entry.py

# Runs as the host user (uid 1000) so outputs on bind mounts stay user-owned.
# Only the writable dirs are chowned: a recursive chown of /app would copy the
# whole ~7 GB venv into a new layer.
# The import check above ran as root and may have created caches under /work.
RUN rm -rf /work && mkdir -p /work/cache /app/.cache /app/checkpoints /app/output /app/logs \
    && chown -R 1000:1000 /work \
    && chown 1000:1000 /app /app/.cache /app/checkpoints /app/output /app/logs
USER 1000:1000

ENV HOME=/work \
    ACESTEP_PROJECT_ROOT=/app \
    ACESTEP_CHECKPOINTS_DIR=/app/checkpoints \
    ACESTEP_TMPDIR=/work/tmp \
    TRITON_CACHE_DIR=/work/cache/triton \
    TORCHINDUCTOR_CACHE_DIR=/work/cache/inductor \
    HF_HOME=/work/cache/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    ACESTEP_API_HOST=127.0.0.1 \
    ACESTEP_API_PORT=18811

EXPOSE 18811
ENTRYPOINT ["python", "/app/gx_engine_entry.py"]
