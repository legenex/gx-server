# =============================================================================
# gx-call engine -- NVIDIA NemotronLabs VoiceChat 11B on NVIDIA GB10
# (aarch64, sm_121, CUDA 13, unified memory).
#
# ENGINE ONLY. It runs `gx_call_engine.py` (a loopback WebSocket server around
# NeMo's StreamingS2SPipeline) and is fronted by the gx-call supervisor
# (legenex/call/gx_call), exactly like ComfyUI behind gx-media-router.
#
# Why not NVIDIA's realtime container: nvcr.io/nim/nvidia/nemotron-labs-voicechat
# is published for linux/amd64 only (manifest checked 2026-09-17) and its vLLM
# engine path needs a patched vLLM (`vllm.config.model.CustomInputSpec`) that
# is not in any public vLLM release. So the NeMo native PyTorch engine is used,
# with the GB10 torch set proven by gx-comfyui / gx-music (torch 2.14 cu130),
# and mamba-ssm / causal-conv1d compiled here for Blackwell against that torch.
#
# GPU ACCESS IS CDI:  docker run --device nvidia.com/gpu=all ...
#
# Build on gx10-02 from the node-2 checkout:
#   docker build -t gx-call-engine:voicechat-097dfe9-t214 legenex/call/engine
# =============================================================================

# ---------------------------------------------------------------- builder --
# The GB10 vLLM image already on gx10-02 carries the CUDA 13.0 toolkit (nvcc).
ARG BUILDER=jstarkg/vllm-gb10-flashnext:0.28-sm121-r6
ARG RUNTIME=nvidia/cuda:13.0.1-runtime-ubuntu24.04
FROM ${BUILDER} AS builder
ARG TORCH_SPEC="torch==2.14.0+cu130"
ENV PIP_NO_CACHE_DIR=1 MAX_JOBS=2 NVCC_THREADS=1 \
    TORCH_CUDA_ARCH_LIST="12.0" \
    CAUSAL_CONV1D_FORCE_BUILD=TRUE MAMBA_FORCE_BUILD=TRUE \
    CAUSAL_CONV1D_SKIP_CUDA_BUILD=FALSE MAMBA_SKIP_CUDA_BUILD=FALSE
RUN uv venv /opt/build --python /usr/bin/python3.12 \
    && uv pip install --python /opt/build/bin/python --index-strategy unsafe-best-match \
         --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple \
         ${TORCH_SPEC} ninja packaging wheel "setuptools>=70" pip
RUN mkdir -p /wheels && cd /wheels \
    && /opt/build/bin/python -m pip wheel --no-build-isolation --no-deps -w /wheels \
         causal-conv1d==1.6.2.post1 \
    && /opt/build/bin/python -m pip wheel --no-build-isolation --no-deps -w /wheels \
         mamba-ssm==2.3.2.post1 \
    && ls -la /wheels

# ---------------------------------------------------------------- runtime --
FROM ${RUNTIME}
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_NO_CACHE_DIR=1
# python3-dev + build-essential: Triton JIT (mamba-ssm's Triton kernels) needs
# Python.h and gcc at run time. libsndfile/sox/ffmpeg: NeMo audio stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv build-essential git ca-certificates curl \
        ffmpeg libsndfile1 sox libsox-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# GB10 torch set (same as gx-comfyui:sm121 and gx-music-engine:*-t214).
ARG TORCH_SPEC="torch==2.14.0+cu130 torchaudio==2.11.0+cu130 torchvision==0.29.0+cu130"
RUN uv venv /opt/gxcall --python /usr/bin/python3.12 \
    && uv pip install --python /opt/gxcall/bin/python --index-strategy unsafe-best-match \
         --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple \
         ${TORCH_SPEC}
ENV PATH=/opt/gxcall/bin:$PATH VIRTUAL_ENV=/opt/gxcall

# NeMo Speech, branch nemotron-labs-voicechat, pinned commit (2026-08-10).
ARG NEMO_REF=097dfe9e2f55baf653b83035868bdc89849f1b47
RUN git clone --filter=blob:none https://github.com/NVIDIA-NeMo/Speech.git /opt/nemo \
    && git -C /opt/nemo checkout --detach ${NEMO_REF} \
    && git -C /opt/nemo rev-parse HEAD > /opt/nemo/.gx-nemo-ref \
    && rm -rf /opt/nemo/.git

COPY requirements-gx.txt /opt/requirements-gx.txt
# Constraints keep the GB10 torch set from being replaced by any dependency.
RUN python -c "import torch, torchaudio, torchvision; open('/opt/constraints.txt','w').write(f'torch=={torch.__version__}\ntorchaudio=={torchaudio.__version__}\ntorchvision=={torchvision.__version__}\n')" \
    && uv pip install --python /opt/gxcall/bin/python --index-strategy unsafe-best-match \
         --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cu130 \
         -c /opt/constraints.txt -r /opt/requirements-gx.txt \
    && uv pip install --python /opt/gxcall/bin/python --no-deps /opt/nemo

COPY --from=builder /wheels /wheels
RUN uv pip install --python /opt/gxcall/bin/python --no-deps /wheels/*.whl && rm -rf /wheels

COPY gx_call_engine.py gx_call_tracker.py gx_voicechat_patches.py /opt/gxcall-app/
ENV PYTHONPATH=/opt/gxcall-app \
    HF_HOME=/work/cache/hf TORCH_HOME=/work/cache/torch NEMO_CACHE_DIR=/work/cache/nemo \
    NUMBA_CACHE_DIR=/work/cache/numba TRITON_CACHE_DIR=/work/cache/triton MPLCONFIGDIR=/work/cache/mpl \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# Fail the BUILD if the Python stack cannot even be imported (no GPU needed).
RUN mkdir -p /work/cache && python - <<'EOF'
import torch, transformers, mamba_ssm, causal_conv1d
print("torch", torch.__version__, "transformers", transformers.__version__,
      "mamba_ssm", mamba_ssm.__version__, "causal_conv1d", causal_conv1d.__version__)
from nemo.collections.speechlm2.inference.factory.s2s_pipeline_builder import S2SPipelineBuilder  # noqa: F401
from nemo.collections.speechlm2.inference.streaming.framing.s2s_request_options import S2SRequestOptions  # noqa: F401
import websockets, jinja2
print("imports ok; websockets", websockets.__version__)
EOF
RUN rm -rf /work/cache /root/.cache && mkdir -p /work/cache && chown -R 1000:1000 /work
WORKDIR /work
USER 1000:1000
ENTRYPOINT ["python", "-u", "/opt/gxcall-app/gx_call_engine.py"]
