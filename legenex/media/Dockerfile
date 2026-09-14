# =============================================================================
# ComfyUI for NVIDIA GB10 (DGX Spark / ASUS GX10) -- aarch64, CUDA 13, sm_121.
#
# This image is the GENERATION ENGINE ONLY. It must never be the ingress:
# ComfyUI's POST /prompt executes an arbitrary node graph and GET /view is an
# unauthenticated file-read primitive. Publish only the router in front of it
# (see docker-compose.media.yml); ComfyUI itself is bound to loopback.
#
# -----------------------------------------------------------------------------
# GPU ACCESS ON THIS CLUSTER IS CDI. There is NO `nvidia` docker runtime:
#     docker run --device nvidia.com/gpu=all ...      <-- correct
#     docker run --gpus all / --runtime nvidia ...    <-- FAILS on these nodes
# -----------------------------------------------------------------------------
#
# MEASURED FACTS THAT SHAPE THIS FILE (verified on gx10-02, not assumed):
#
#  1. sm_121 needs no special build. torch cu130 ships sm_120 cubins and no PTX
#     (`get_arch_list()` -> sm_80/90/100/110/120). GB10 reports capability
#     (12,1); the sm_120 cubins execute correctly on it (same Blackwell major).
#     TORCH_CUDA_ARCH_LIST=12.1 and a SageAttention source build are NOT needed.
#  2. download.pytorch.org/whl/cu130 DOES serve cp312 aarch64 wheels. The proven
#     set is torch 2.14.0+cu130 / torchvision 0.29.0+cu130 / torchaudio
#     2.11.0+cu130. Do not pin lower: a low torch pin pairs with a mismatched
#     torchaudio whose native library then fails to load.
#  3. Triton JIT-compiles a CUDA shim on the first text-encode and needs
#     Python.h -> python3-dev is LOAD-BEARING, not optional.
#  4. ComfyUI's requirements.txt lists torch/torchvision/torchaudio. They MUST
#     be filtered out or pip replaces the cu130 build with a generic one.
#     torchsde is pure Python and is kept.
#  5. No aarch64 onnxruntime-gpu wheel exists; ControlNet preprocessors would
#     fall back to CPU. Not used by any shipped workflow.
#  6. Never pass --force-fp16 (NaNs on Blackwell). Avoid --gpu-only/--highvram:
#     this is a 121 GiB UNIFIED memory node, CPU and GPU compete for it.
# =============================================================================

ARG CUDA_TAG=13.0.1-runtime-ubuntu24.04
FROM nvidia/cuda:${CUDA_TAG}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3-dev + build-essential: see finding 3 (Triton needs Python.h and gcc).
# ffmpeg/libgl/libglib: video muxing and image codecs for the video workflows.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv python3-pip \
        build-essential git ca-certificates curl \
        ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the exact upstream commit proven on this hardware. Override only
# deliberately; a floating master is not reproducible.
ARG COMFY_REF=b00584967778871f329c2d31190a5e9b767b8366
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /opt/comfyui \
    && git -C /opt/comfyui checkout --detach ${COMFY_REF} \
    && rm -rf /opt/comfyui/.git

WORKDIR /opt/comfyui
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --upgrade pip wheel setuptools

# cu130 aarch64 wheels. Left unpinned so pip resolves a mutually consistent
# torch/torchvision/torchaudio trio from the cu130 index (finding 2).
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchvision torchaudio

# Strip torch* so the cu130 build survives the requirements install (finding 4).
RUN grep -vE '^(torch|torchvision|torchaudio)$' requirements.txt > /tmp/req.txt \
    && pip install --timeout 60 --retries 10 -r /tmp/req.txt \
    && rm /tmp/req.txt

# Fail the BUILD, not the first generation, if the CUDA stack is wrong.
RUN python -c "import torch, torchvision, torchaudio; \
print('torch', torch.__version__, 'tv', torchvision.__version__, 'ta', torchaudio.__version__); \
assert torch.version.cuda and torch.version.cuda.startswith('13'), 'expected a CUDA 13 build'; \
al = torch.cuda.get_arch_list(); print('arch list', al); \
assert any(a in al for a in ('sm_120','sm_121')), 'no Blackwell cubin in this wheel'"

# Run as uid/gid 1000 so files written to the bind-mounted /srv/models are owned
# by the host service account and not by root (we have no root on these nodes).
ARG RUN_UID=1000
ARG RUN_GID=1000
RUN groupadd -g ${RUN_GID} comfy 2>/dev/null || true \
    && useradd -m -u ${RUN_UID} -g ${RUN_GID} -s /usr/sbin/nologin comfy 2>/dev/null || true \
    && mkdir -p /srv/cache /srv/logs \
    && chown -R ${RUN_UID}:${RUN_GID} /opt/comfyui /srv/cache /srv/logs
USER ${RUN_UID}:${RUN_GID}

ENV HF_HOME=/srv/cache/hf \
    TRITON_CACHE_DIR=/srv/cache/triton \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

EXPOSE 8188

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8188/system_stats || exit 1

# 0.0.0.0 is INSIDE the container only. The compose file puts this service on an
# internal bridge and publishes it to 127.0.0.1 on the host -- never to a routable
# address. See finding 6 for the flags deliberately absent here.
ENTRYPOINT ["python", "main.py", \
            "--listen", "0.0.0.0", "--port", "8188", \
            "--output-directory", "/srv/models/comfy-output", \
            "--input-directory",  "/srv/models/comfy-input", \
            "--temp-directory",   "/srv/models/comfy-temp", \
            "--user-directory",   "/srv/models/comfy-user"]
