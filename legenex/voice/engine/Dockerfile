# =============================================================================
# gx-voice engine -- Qwen3-TTS 12Hz 1.7B (CustomVoice / VoiceDesign / Base)
# for NVIDIA GB10 (aarch64, CUDA 13, sm_121).
#
# ENGINE ONLY. gx_voice_engine.py is a small loopback HTTP server that owns
# the model objects. It is started on demand by the gx-voice supervisor
# (legenex/voice/gx_voice) through the node-2 admission guard, and it is
# never reachable from the network: the supervisor publishes it on
# 127.0.0.1 only and authenticates with a per-host random key.
#
# GPU ACCESS IS CDI:  docker run --device nvidia.com/gpu=all ...
#
# GB10 TORCH: the stack proven on this hardware by gx-comfyui and gx-music
# (torch 2.14.0 / torchaudio 2.11.0, cu130). Upstream qwen-tts pins only
# transformers==4.57.3 and accelerate==1.12.0; those pins are kept.
# FlashAttention 2 has no aarch64/sm_121 wheel; the engine uses PyTorch SDPA.
# gradio (only needed by upstream's demo CLI) is not installed.
#
#   docker build -t gx-voice-engine:qwen3tts-022e286-t214 legenex/voice/engine
#   legenex/voice/scripts/engine-selftest.sh
# =============================================================================
ARG CUDA_TAG=13.0.1-runtime-ubuntu24.04
FROM nvidia/cuda:${CUDA_TAG}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# ffmpeg: encoding (mp3/flac/opus/aac), time stretch and decoding of uploaded
# reference audio. sox + libsndfile: soundfile/librosa and qwen_tts imports.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv \
        build-essential git ca-certificates curl \
        ffmpeg libsndfile1 sox \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# Official runtime, pinned (github.com/QwenLM/Qwen3-TTS, qwen-tts 0.1.1).
ARG QWEN_TTS_REF=022e286b98fbec7e1e916cb940cdf532cd9f488e
RUN git clone https://github.com/QwenLM/Qwen3-TTS.git /opt/qwen3-tts \
    && git -C /opt/qwen3-tts checkout --detach ${QWEN_TTS_REF} \
    && git -C /opt/qwen3-tts rev-parse HEAD > /opt/qwen3-tts/.gx-ref \
    && rm -rf /opt/qwen3-tts/.git

ARG TORCH_SPEC="torch==2.14.0+cu130 torchaudio==2.11.0+cu130"
RUN uv venv /opt/venv --python /usr/bin/python3.12 \
    && uv pip install --python /opt/venv/bin/python --index-strategy unsafe-best-match \
         --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple \
         ${TORCH_SPEC} \
         "transformers==4.57.3" "accelerate==1.12.0" "huggingface_hub<1.0" \
         librosa soundfile sox onnxruntime einops numpy \
    && uv pip install --python /opt/venv/bin/python --no-deps /opt/qwen3-tts \
    && rm -rf /root/.cache

ENV PATH=/opt/venv/bin:$PATH

# Fail the BUILD if the stack is wrong (the GPU check runs at deploy time:
# scripts/engine-selftest.sh).
RUN python -c "import torch, torchaudio, transformers; \
print('torch', torch.__version__, 'ta', torchaudio.__version__, 'tf', transformers.__version__, 'cuda', torch.version.cuda); \
assert torch.version.cuda and torch.version.cuda.startswith('13'), 'expected CUDA 13'; \
assert torch.__version__.startswith('2.14'), 'GB10 torch override missing'; \
assert transformers.__version__ == '4.57.3', 'qwen-tts pins transformers 4.57.3'" \
 && python -c "from qwen_tts import Qwen3TTSModel; print('qwen_tts import ok')"

COPY gx_voice_engine.py /opt/gx-voice/gx_voice_engine.py

# Runs as the host user (uid 1000) so outputs on bind mounts stay user-owned.
RUN rm -rf /work && mkdir -p /work/cache /work/data \
    && chown -R 1000:1000 /work
USER 1000:1000

ENV HOME=/work \
    HF_HOME=/work/cache/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    NUMBA_CACHE_DIR=/work/cache/numba \
    MPLCONFIGDIR=/work/cache/mpl \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    GX_VOICE_ENGINE_HOST=127.0.0.1 \
    GX_VOICE_ENGINE_PORT=18831

EXPOSE 18831
ENTRYPOINT ["python", "/opt/gx-voice/gx_voice_engine.py"]
