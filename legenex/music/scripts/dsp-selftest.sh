#!/usr/bin/env bash
# Run the measured-analysis tests inside the gx-music engine image (numpy,
# scipy, ffmpeg), exactly where the analysis runs in production. CPU only,
# no network, read-only source mount. Usage (gx10-02 or any host with the image):
#   scripts/dsp-selftest.sh [image]
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
image="${1:-${GX_MUSIC_IMAGE:-gx-music-engine:acestep15-ca1e85f-t214}}"
docker image inspect "$image" >/dev/null
exec docker run --rm --network none --user "$(id -u):$(id -g)" --memory 4g --cpus 4 \
  --read-only --tmpfs /tmp:rw,size=64m -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$here:/src:ro" -w /src --entrypoint "${GX_MUSIC_ANALYSIS_PYTHON:-/app/.venv/bin/python}" \
  "$image" -m unittest tests.test_analysis_dsp -v
