#!/usr/bin/env python3
"""gx-image live acceptance (Build V3, workstream IMG).

Runs the real generations and edits that decide whether image editing works,
and judges them with numbers instead of impressions: every edit is compared
with its own source (SSIM, perceptual-hash distance, colour histogram) by
``image_eval.py``, every output file must be a real PNG of non-zero size, and
a case that comes back as a near-duplicate of its source FAILS.

A MASKED case is judged on the mask's own regions instead, because the
whole-image numbers measure the size of the mask rather than the quality of the
edit (half the frame replaced perfectly still scores ssim ~0.5; a small mask
repainted completely still scores ssim ~1.0). It passes only when the white
"may change" region really changed AND the black region still holds the source.

It never starts a container itself: everything goes through the media router
on gx10-02, which owns ComfyUI admission, the node flock and the 30 GiB
reserve. It refuses to start while a gx-max or Maintenance hold exists.

    ./image_accept.py                      # every case
    ./image_accept.py --only edit_background,edit_style
    ./image_accept.py --list               # print the plan and exit
    ./image_accept.py --out DIR            # default /srv/logs/acceptance/build-v3/img/<UTC stamp>

Needs the venv with numpy/pillow/scikit-image/imagehash:

    ~/.venvs/gx-img-eval/bin/python legenex/media/tools/image_accept.py

Each case writes <name>.png, <name>.json (router metadata + metrics) and, for
edits, keeps the source it was measured against. `summary.json` and
`RESULTS.md` collect the verdicts.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE / "image_eval.py"
REPO = HERE.parents[2]
GUARD = Path("/srv/projects/gx-cluster/state/guard")
DEFAULT_ROOT = Path("/srv/logs/acceptance/build-v3/img")
#: a result this close to its source is a returned copy, not an edit
NEAR_DUP_SSIM = 0.90
NEAR_DUP_PHASH = 8
#: an edit that really changed something clears at least one of these
GOOD_SSIM = 0.88
GOOD_PHASH = 10
#: masked cases only -- inside the mask: Qwen repaints a region it was told to keep at
#: about MAD 2-3 (measured on the halves of edit_clothing/edit_add it was told to keep),
#: so an edit that genuinely painted something new must be clear of that floor
MASK_CHANGED_MAD = 6.0
#: masked cases only -- outside the mask: the workflow composites those pixels back from
#: the source, so this is ~0; the allowance is for the grown, feathered mask edge
MASK_KEPT_MAD = 8.0


@dataclass
class Case:
    name: str
    what: str
    kind: str                      # generate | edit
    args: list[str] = field(default_factory=list)
    source: str | None = None      # the name of an earlier generate case
    mask: str | None = None        # "lower", "upper", "centre"
    #: a near-duplicate of the source is the expected result (control cases)
    expect_near_duplicate: bool = False


SOURCES = [
    Case("src_portrait", "Source A: a person in a room (Qwen Image 2512)", "generate",
         ["--prompt", "photo of a woman in a red jacket standing in a plain office, "
                      "window light, 50mm, sharp focus", "--seed", "424242", "--size", "1024x1024"]),
    Case("src_street", "Source B: a street scene (Qwen Image 2512)", "generate",
         ["--prompt", "photo of an empty city street at noon, parked blue car, brick buildings, clear sky",
          "--seed", "515151", "--size", "1024x1024"]),
    Case("src_vm", "Source C: VisionmasterPro_V3 text-to-image", "generate",
         ["--image-model", "visionmaster-pro-v3", "--size", "832x1216", "--seed", "606060",
          "--prompt", "portrait photo of a man in a grey coat on a station platform, overcast daylight"]),
]

CASES = [
    # (a) object and background replacement
    Case("edit_background", "(a) background replacement: office -> beach at sunset", "edit",
         ["--mode", "background", "--prompt", "a beach at sunset with breaking waves"], source="src_portrait"),
    Case("edit_object", "(a) object replacement: the blue car becomes a yellow taxi", "edit",
         ["--mode", "change", "--prompt", "replace the blue car with a yellow taxi"], source="src_street"),
    Case("edit_remove", "(a) object removal: remove the car and fill the road", "edit",
         ["--mode", "remove", "--prompt", "the blue car"], source="src_street"),
    # (b) style transformation
    Case("edit_style", "(b) style: redraw as a watercolour painting", "edit",
         ["--mode", "restyle", "--prompt", "a loose watercolour painting with visible brush strokes"],
         source="src_portrait"),
    Case("edit_transform", "(b) full transformation at strength 1.0", "edit",
         ["--mode", "transform", "--strength", "1.0",
          "--prompt", "the same scene as an oil painting at night, lit by street lamps"],
         source="src_street"),
    # (c) subject / clothing / object modification
    Case("edit_clothing", "(c) clothing: the red jacket becomes a black leather jacket", "edit",
         ["--mode", "subject", "--prompt", "she wears a black leather jacket instead of the red one"],
         source="src_portrait"),
    Case("edit_add", "(c) add: put a cat on the desk", "edit",
         ["--mode", "add", "--prompt", "a ginger cat sitting in the lower left of the frame"],
         source="src_portrait"),
    # masks and inpainting
    Case("edit_masked_lower", "mask: change only the painted lower half (Qwen, masked template)", "edit",
         ["--mode", "change", "--prompt", "tall green grass"], source="src_portrait", mask="lower"),
    Case("edit_vm_inpaint", "mask: VisionmasterPro_V3 inpaint of the upper half", "edit",
         ["--image-model", "visionmaster-pro-v3", "--mode", "change", "--strength", "0.9",
          "--prompt", "a dense forest canopy"], source="src_vm", mask="upper"),
    Case("edit_vm_img2img", "VisionmasterPro_V3 image-to-image restyle at strength 0.75", "edit",
         ["--image-model", "visionmaster-pro-v3", "--mode", "restyle", "--strength", "0.75",
          "--prompt", "the same man drawn as a charcoal sketch on textured paper"], source="src_vm"),
    # variation
    Case("variation_high", "variation at strength 0.9 (reference latent dropped)", "edit",
         ["--mode", "transform", "--strength", "0.9",
          "--prompt", "a variation of this scene with different details"], source="src_street"),
    # the adapter A/B: the Playground now leaves the edit adapter off by default
    Case("edit_adapter_off", "A/B: background edit with adapter_strength 0 (the new default)", "edit",
         ["--mode", "background", "--adapter", "0",
          "--prompt", "a snowy mountain ridge"], source="src_portrait"),
    Case("edit_adapter_on", "A/B: the same edit with adapter_strength 0.8 (the old default)", "edit",
         ["--mode", "background", "--adapter", "0.8",
          "--prompt", "a snowy mountain ridge"], source="src_portrait"),
    # the regression control: the bug was strength 0.6 bound to KSampler.denoise
    Case("regression_strength", "control: strength 0.6 on an instruction edit must be IGNORED, "
                                "so this must still change the picture", "edit",
         ["--mode", "change", "--strength", "0.6",
          "--prompt", "replace the red jacket with a yellow raincoat"], source="src_portrait"),
]

ALL = {c.name: c for c in SOURCES + CASES}


def png_mask(width: int, height: int, region: str) -> bytes:
    """A white-on-black PNG mask: white marks the region the edit may change."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if region == "lower":
                on = y >= height // 2
            elif region == "upper":
                on = y < height // 2
            else:
                on = abs(x - width // 2) < width // 4 and abs(y - height // 2) < height // 4
            rows += bytes([255, 255, 255] if on else [0, 0, 0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b""))


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:33]
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit(f"{path} is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def holds() -> list[str]:
    return [p.name for p in (GUARD / "node2.gxmax-hold", GUARD / "node2.maintenance-hold") if p.exists()]


def run_eval(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, str(EVAL), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:2000]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"unreadable output: {proc.stdout[:500]}"}


def masked_verdict(case: Case, m: dict) -> tuple[str, str]:
    """Judge a masked edit on the mask's regions: white had to change, black had to survive."""
    phash = m.get("phash_masked_dist") or 0
    detail = (f"masked: ssim {m['ssim_masked']} phash {phash} mad_white {m['mad_masked_white']} "
              f"mad_black {m['mad_masked_black']} (whole image: ssim {m.get('ssim')} "
              f"phash {m.get('phash_dist')} mad {m.get('mad')})")
    near = (m["ssim_masked"] >= NEAR_DUP_SSIM and phash <= NEAR_DUP_PHASH) \
        or m["mad_masked_white"] < MASK_CHANGED_MAD
    if case.expect_near_duplicate:
        return ("PASS" if near else "FAIL"), detail + " (expected a near-duplicate)"
    if m["mad_masked_black"] > MASK_KEPT_MAD:
        return "FAIL", detail + " -> the area the mask protects was changed as well"
    if near:
        return "FAIL", detail + " -> the masked area came back as a copy of the source"
    if m["ssim_masked"] > GOOD_SSIM and phash < GOOD_PHASH:
        return "WEAK", detail + " -> the masked area changed, but barely; look at the images"
    return "PASS", detail


def verdict(case: Case, payload: dict, out: Path) -> tuple[str, str]:
    if "error" in payload:
        return "FAIL", payload["error"][:300]
    if not out.is_file() or out.stat().st_size == 0:
        return "FAIL", "no output file, or the output file is empty"
    if case.kind == "generate":
        return "PASS", f"{png_size(out)[0]}x{png_size(out)[1]}, {out.stat().st_size} bytes"
    m = payload.get("metrics") or {}
    if case.mask and m.get("ssim_masked") is not None:
        return masked_verdict(case, m)
    near = bool(m.get("near_duplicate"))
    detail = (f"ssim {m.get('ssim')} phash {m.get('phash_dist')} hist {m.get('hist_corr')} "
              f"mad {m.get('mad')}")
    if case.expect_near_duplicate:
        return ("PASS" if near else "FAIL"), detail + " (expected a near-duplicate)"
    if near:
        return "FAIL", detail + " -> the edit returned a near-copy of the source"
    if m.get("ssim", 1.0) > GOOD_SSIM and (m.get("phash_dist") or 0) < GOOD_PHASH:
        return "WEAK", detail + " -> it changed, but barely; look at the images"
    return "PASS", detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", default="", help="comma-separated case names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--ignore-holds", action="store_true",
                    help="run although a gx-max / Maintenance hold exists (say why in the report)")
    args = ap.parse_args()

    if args.list:
        for case in SOURCES + CASES:
            print(f"{case.name:22s} {case.kind:8s} {case.what}")
        return 0
    held = holds()
    if held and not args.ignore_holds:
        print(f"refusing to run: {', '.join(held)} exists on gx10-01. "
              f"Wait for the hold to clear (BUILD_V3 rule 2).", file=sys.stderr)
        return 2

    wanted = {n.strip() for n in args.only.split(",") if n.strip()}
    if wanted - set(ALL):
        print(f"unknown case(s): {', '.join(sorted(wanted - set(ALL)))}", file=sys.stderr)
        return 2
    cases = [c for c in CASES if not wanted or c.name in wanted]
    needed = {c.source for c in cases if c.source} | {c.name for c in SOURCES if not wanted or c.name in wanted}
    sources = [c for c in SOURCES if c.name in needed]

    out_dir = args.out or DEFAULT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.time()
    for case in sources + cases:
        out = out_dir / f"{case.name}.png"
        argv = [case.kind, *( [str(out_dir / f"{case.source}.png"), str(out)] if case.kind == "edit"
                              else [str(out)] ), *case.args]
        if case.kind == "edit":
            src = out_dir / f"{case.source}.png"
            if not src.is_file():
                results.append({"case": case.name, "what": case.what, "verdict": "SKIP",
                                "detail": f"source {case.source} is missing"})
                print(f"SKIP {case.name}: source missing")
                continue
            if case.mask:
                mask_path = out_dir / f"{case.name}.mask.png"
                mask_path.write_bytes(png_mask(*png_size(src), case.mask))
                argv += ["--mask", str(mask_path)]
        print(f"... {case.name}: {case.what}", flush=True)
        t0 = time.time()
        payload = run_eval(argv)
        status, detail = verdict(case, payload, out)
        record = {"case": case.name, "what": case.what, "kind": case.kind, "verdict": status,
                  "detail": detail, "seconds": round(time.time() - t0, 1),
                  "source": case.source, "mask": case.mask,
                  "bytes": out.stat().st_size if out.is_file() else 0,
                  "router": payload.get("gx"), "metrics": payload.get("metrics"),
                  "error": payload.get("error")}
        (out_dir / f"{case.name}.json").write_text(json.dumps(record, indent=1) + "\n")
        results.append(record)
        print(f"{status:5s} {case.name}: {detail}", flush=True)

    summary = {"started": started, "finished": time.time(), "node": "gx10-02",
               "holds_at_start": held, "repo": str(REPO), "results": results,
               "counts": {v: sum(1 for r in results if r["verdict"] == v)
                          for v in ("PASS", "WEAK", "FAIL", "SKIP")}}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    lines = ["# gx-image live acceptance (Build V3, IMG)", "",
             f"Run: {datetime.now(timezone.utc).isoformat(timespec='seconds')} · router gx10-02 · "
             f"evidence `{out_dir}`", "",
             "| Case | What | Verdict | Evidence |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| `{r['case']}` | {r['what']} | **{r['verdict']}** | {r['detail']} |")
    lines += ["", "A verdict of FAIL on an edit means the result was a near-duplicate of its source "
                  "(ssim >= 0.90 and phash distance <= 8), an empty file, or a router error.",
              "", "A masked case is judged inside the mask instead: it FAILS when the masked region came "
                  f"back as a copy (ssim_masked >= {NEAR_DUP_SSIM} and phash <= {NEAR_DUP_PHASH}, or "
                  f"mad_masked_white < {MASK_CHANGED_MAD}), or when the region the mask protects moved "
                  f"(mad_masked_black > {MASK_KEPT_MAD}).", ""]
    (out_dir / "RESULTS.md").write_text("\n".join(lines))
    print(f"\n{summary['counts']}\nevidence: {out_dir}")
    return 1 if summary["counts"]["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
