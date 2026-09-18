#!/usr/bin/env python3
"""Image-edit acceptance harness for gx-image (Build V3, workstream IMG).

Runs real generations and edits against the media router over the fabric and
measures how different each edit is from its source, so "the edit came back
as a copy of the input" is caught by numbers, not by eye alone.

Metrics (source vs. result, both resampled to the same size):
  * ssim        structural similarity of the luminance (1.0 = identical)
  * phash_dist  64-bit perceptual-hash Hamming distance (0 = same picture)
  * hist_corr   correlation of the 3-D colour histograms (1.0 = same palette)
  * mad         mean absolute difference, 0..255 (context only, never the verdict)
A result is a NEAR-DUPLICATE when ssim >= 0.90 and phash_dist <= 8.

With a --mask the same numbers are ALSO measured per region, because a
whole-image metric cannot judge a masked edit: replacing half the picture
perfectly still leaves ssim near 0.5, and a small mask repainted completely
still leaves ssim near 1.0, so the whole-image verdict is about the mask's size
rather than about the edit. The extra fields are
  * ssim_masked          SSIM averaged over the white (may change) region only
  * phash_masked_dist    pHash distance of the white region's bounding box
  * mad_masked_white     mean absolute difference inside the mask (must be large)
  * mad_masked_black     mean absolute difference outside it (must be ~0: the
                         workflow composites those pixels back from the source)
  * mask_white_fraction  how much of the frame the mask covers

Needs numpy, pillow, scikit-image and imagehash (the venv
~/.venvs/gx-img-eval on gx10-01 has them). The router key is read from
legenex/gateway/.env and is never printed.

Usage:
  image_eval.py generate OUT.png --prompt TEXT [--workflow NAME] [--image-model ID] [--seed N] [--size WxH]
  image_eval.py edit SRC.png OUT.png --prompt TEXT [--mode MODE] [--strength F] [--seed N] [--mask MASK.png] [--image-model ID]
  image_eval.py compare SRC.png OUT.png [--mask MASK.png]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROUTER = "http://192.168.100.11:18800"
ENV_FILE = Path(__file__).resolve().parents[2] / "gateway" / ".env"
NEAR_DUP_SSIM = 0.90
NEAR_DUP_PHASH = 8


def _key() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("GX_MEDIA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("GX_MEDIA_API_KEY not found in legenex/gateway/.env")


def _post(path: str, body: dict, timeout: float = 2400) -> dict:
    req = urllib.request.Request(ROUTER + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 - fixed fabric URL
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"router HTTP {exc.code}: {exc.read()[:800].decode(errors='replace')}") from None


def _save(result: dict, out: Path) -> dict:
    data = result.get("data") or []
    if not data:
        raise SystemExit("router returned no image")
    out.write_bytes(base64.b64decode(data[0]["b64_json"]))
    return result.get("gx") or {}


def compare(src: Path, out: Path, mask: Path | None = None) -> dict:
    import imagehash
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity

    a = Image.open(src).convert("RGB")
    b = Image.open(out).convert("RGB").resize(a.size, Image.LANCZOS)
    size = (512, round(512 * a.height / a.width))
    a_s, b_s = a.resize(size, Image.LANCZOS), b.resize(size, Image.LANCZOS)
    ga = np.asarray(a_s.convert("L"), dtype=np.float64)
    gb = np.asarray(b_s.convert("L"), dtype=np.float64)
    ssim, ssim_map = structural_similarity(ga, gb, data_range=255, full=True)
    ssim = float(ssim)
    phash = int(imagehash.phash(a) - imagehash.phash(b))

    def hist(img: Image.Image) -> np.ndarray:
        arr = np.asarray(img).reshape(-1, 3) // 32
        h = np.bincount(arr[:, 0] * 64 + arr[:, 1] * 8 + arr[:, 2], minlength=512).astype(np.float64)
        return h / h.sum()

    ha, hb = hist(a_s), hist(b_s)
    corr = float(np.corrcoef(ha, hb)[0, 1])
    diff = np.abs(np.asarray(a_s, dtype=np.float64) - np.asarray(b_s, dtype=np.float64))
    mad = float(diff.mean())
    metrics = {"ssim": round(ssim, 4), "phash_dist": phash, "hist_corr": round(corr, 4), "mad": round(mad, 2),
               "near_duplicate": ssim >= NEAR_DUP_SSIM and phash <= NEAR_DUP_PHASH}
    if mask is not None:
        metrics.update(_masked_metrics(a, b, a_s.size, ssim_map, diff, mask))
    return metrics


def _masked_metrics(a, b, size: tuple[int, int], ssim_map, diff, mask: Path) -> dict:
    """Per-region numbers for a masked edit: white = may change, black = must not.

    White is "may change" throughout the stack (the mask PNG the harness paints, the
    router's mask_image and the workflow's SetLatentNoiseMask), so the white region is
    where the edit has to show and the black region is where the source must survive.
    """
    import imagehash
    import numpy as np
    from PIL import Image

    m_full = Image.open(mask).convert("L")
    if m_full.size != a.size:
        m_full = m_full.resize(a.size, Image.NEAREST)
    white_full = np.asarray(m_full) > 127
    # NEAREST keeps the mask binary at the metric working size
    white = np.asarray(m_full.resize(size, Image.NEAREST)) > 127
    frac = float(white.mean())
    if frac <= 0.0 or frac >= 1.0:  # a mask that selects everything or nothing tells us nothing
        return {"mask_white_fraction": round(frac, 4)}
    ssim_masked = float(ssim_map[white].mean())
    per_pixel = diff.mean(axis=2)
    out = {"mask_white_fraction": round(frac, 4),
           "ssim_masked": round(ssim_masked, 4),
           "ssim_unmasked": round(float(ssim_map[~white].mean()), 4),
           "mad_masked_white": round(float(per_pixel[white].mean()), 2),
           "mad_masked_black": round(float(per_pixel[~white].mean()), 2)}
    ys, xs = np.nonzero(white_full)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    phash_masked = int(imagehash.phash(a.crop(box)) - imagehash.phash(b.crop(box)))
    out["phash_masked_dist"] = phash_masked
    out["masked_near_duplicate"] = ssim_masked >= NEAR_DUP_SSIM and phash_masked <= NEAR_DUP_PHASH
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("out", type=Path)
    g.add_argument("--prompt", required=True)
    g.add_argument("--negative", default=None)
    g.add_argument("--workflow", default=None)
    g.add_argument("--image-model", default=None)
    g.add_argument("--seed", type=int, default=None)
    g.add_argument("--size", default="1024x1024")
    g.add_argument("--quality", default=None)
    e = sub.add_parser("edit")
    e.add_argument("src", type=Path)
    e.add_argument("out", type=Path)
    e.add_argument("--prompt", required=True)
    e.add_argument("--mode", default=None)
    e.add_argument("--strength", type=float, default=None)
    e.add_argument("--seed", type=int, default=None)
    e.add_argument("--workflow", default=None)
    e.add_argument("--image-model", default=None)
    e.add_argument("--mask", type=Path, default=None)
    e.add_argument("--adapter", type=float, default=None)
    c = sub.add_parser("compare")
    c.add_argument("src", type=Path)
    c.add_argument("out", type=Path)
    c.add_argument("--mask", type=Path, default=None)
    args = ap.parse_args()

    if args.cmd == "compare":
        print(json.dumps(compare(args.src, args.out, args.mask)))
        return 0
    body: dict = {"prompt": args.prompt, "response_format": "b64_json"}
    if args.seed is not None:
        body["seed"] = args.seed
    if args.workflow:
        body["workflow"] = args.workflow
    if args.image_model:
        body["image_model"] = args.image_model
    t0 = time.time()
    if args.cmd == "generate":
        body["size"] = args.size
        if args.negative:
            body["negative_prompt"] = args.negative
        if args.quality:
            body["quality"] = args.quality
        gx = _save(_post("/v1/images/generations", body), args.out)
        print(json.dumps({"elapsed": round(time.time() - t0, 1), "gx": gx}))
        return 0
    body["image"] = base64.b64encode(args.src.read_bytes()).decode()
    if args.mode:
        body["edit_mode"] = args.mode
    if args.strength is not None:
        body["strength"] = args.strength
    if args.adapter is not None:
        body["adapter_strength"] = args.adapter
    if args.mask:
        body["mask"] = base64.b64encode(args.mask.read_bytes()).decode()
    gx = _save(_post("/v1/images/edits", body), args.out)
    print(json.dumps({"elapsed": round(time.time() - t0, 1), "gx": gx,
                      "metrics": compare(args.src, args.out, args.mask)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
