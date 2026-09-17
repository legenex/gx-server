# gx-live engine image: MiniCPM-o 4.5 (transformers remote code) on GB10.
#
# Base: the gx-music engine image, which already carries the torch build that
# is proven on GB10 / sm_121 (torch 2.14.0+cu130, torchaudio 2.11, torchvision
# 0.29) in /app/.venv. Upstream MiniCPM-o pins torch<=2.8; those cu128 wheels do
# not target sm_121, so the proven torch stays and only the model's Python
# dependencies are added on top.
#
#   cd legenex/live/engine
#   docker build -t gx-live-engine:minicpmo45-503e754-t214 .
#
# Pinned (model card 503e754 + minicpmo-utils 1.0.6):
#   transformers 4.51.0 (the model card's tested version; <4.53 required)
#   minicpmo-utils 1.0.6 without its video extras (decord has no aarch64
#   wheel and the engine receives decoded frames, never video files)
#   onnxruntime 1.21.0 (CPU; campplus speaker embedding)
#   ruamel.yaml 0.18.10 (0.19 breaks hyperpyyaml 1.2.2: 'Loader' has no max_depth)
#   silero-vad 6.2.1 (MIT; bundled ONNX VAD model, CPU)
# No flash-attn: there is no sm_121 build; the engine uses SDPA.
ARG BASE=gx-music-engine:acestep15-ca1e85f-t214
FROM ${BASE}
USER 0
RUN uv pip install --no-cache --python /app/.venv/bin/python \
        "transformers==4.51.0" "tokenizers>=0.21,<0.22" \
        "onnxruntime==1.21.0" "onnx==1.17.0" "hyperpyyaml==1.2.2" "ruamel.yaml==0.18.10" \
        "librosa==0.10.2.post1" "soundfile==0.13.1" "einops==0.8.1" "xxhash==3.5.0" \
    && uv pip install --no-cache --no-deps --python /app/.venv/bin/python \
        "minicpmo-utils==1.0.6" "silero-vad==6.2.1" \
    && python -c "import torch, transformers, onnxruntime, stepaudio2, s3tokenizer, librosa; \
from silero_vad import load_silero_vad; load_silero_vad(onnx=True); \
assert torch.__version__.startswith('2.14'), torch.__version__; \
assert transformers.__version__ == '4.51.0', transformers.__version__; \
print('gx-live engine deps ok', torch.__version__, transformers.__version__, onnxruntime.__version__)"
# The engine code is mounted read-only from the node-2 checkout at run time
# (/opt/gx-live/gx_live_engine); weights are mounted from /srv/models/live.
RUN rm -rf /work/cache /work/.cache /work/.config /work/tmp \
    && mkdir -p /work/cache /work/tmp && chown -R 1000:1000 /work
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/work MPLCONFIGDIR=/work/cache/mpl PYTHONPATH=/opt/gx-live
WORKDIR /opt/gx-live
USER 1000:1000
ENTRYPOINT ["python", "-m", "gx_live_engine"]
