"""gx-music engine entrypoint: upstream acestep.api_server with two local policies.

1. OFFLINE / NO SURPRISE DOWNLOADS. Upstream treats the full "main model"
   bundle (2B turbo DiT + 1.7B LM + VAE + text encoder) as mandatory and
   auto-downloads it when any part is missing. gx-music ships only the parts it
   uses (XL turbo DiT, 4B LM, VAE, text encoder), so the mandatory set is
   narrowed to the shared components. HF_HUB_OFFLINE=1 is also set by the
   image, so a missing checkpoint fails loudly instead of pulling ~10 GB.
2. Nothing else is patched. All generation behaviour is upstream's.
"""
import os
import sys

from acestep import model_downloader

model_downloader.MAIN_MODEL_COMPONENTS[:] = ["vae", "Qwen3-Embedding-0.6B"]
model_downloader.DEFAULT_LM_MODEL = os.environ.get("ACESTEP_LM_MODEL_PATH", "acestep-5Hz-lm-4B")

for d in (os.environ.get("ACESTEP_TMPDIR", "/work/tmp"),
          os.environ.get("TRITON_CACHE_DIR", "/work/cache/triton")):
    os.makedirs(d, exist_ok=True)

from acestep.api_server import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--host", os.environ.get("ACESTEP_API_HOST", "127.0.0.1"),
                "--port", os.environ.get("ACESTEP_API_PORT", "18811")] + sys.argv[1:]
    main()
