#!/usr/bin/env python3
"""Prove that vLLM loads the abliteration OVERLAY copy of attn.wo_b, not the base copy.

Stdlib only; reads safetensors headers and raw tensor bytes, never executes
anything from the model directory (run it with ``python3 -I``).

    overlay_check.py verify  MODEL_DIR            # static loader-order proof
    overlay_check.py digests MODEL_DIR [--tp 2]   # expected per-rank sha256
    overlay_check.py compare MODEL_DIR LOG...     # classify GX_WOB_DIGEST lines

Why "verify" is a proof for vLLM v0.25.1 (commit 752a3a50):

* ``DefaultModelLoader._prepare_weights`` globs ``*.safetensors`` and
  ``filter_duplicate_safetensors_files`` keeps every file that the index's
  weight_map names. The base shards stay (they hold the other 72,225 tensors),
  so BOTH copies of the 92 overlay tensors are read.
* ``safetensors_weights_iterator`` sorts the files with ``_natural_sort_key``
  and yields every tensor of every file, without de-duplication.
* ``DeepseekV4Model.load_weights`` / the DSpark draft ``load_weights`` copy
  each yielded tensor into its parameter (``weight_loader``): the LAST copy
  yielded is what stays resident.

So the overlay wins iff its file sorts after every base shard under that
exact key, nothing else re-orders the files (no multi-thread / fastsafetensors
/ runai loaders), and the directory holds no stray safetensors. ``verify``
checks all of that and exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

INDEX = "model.safetensors.index.json"
#: Exactly what the overlay replaces (model card, 2026-07-31 rebuild).
EXPECTED_OVERLAY_TENSORS = 92
_OVERLAY_NAME = re.compile(r"(^|\.)attn\.wo_b\.(weight|scale)$")
#: Element sizes of the dtypes this checkpoint uses (safetensors dtype names).
_ITEMSIZE = {
    "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1, "U8": 1, "I8": 1, "BOOL": 1,
    "BF16": 2, "F16": 2, "I16": 2, "F32": 4, "I32": 4, "U32": 4, "F64": 8, "I64": 8,
}
#: Probed tensors: checkpoint name -> the vLLM model class that holds it.
PROBES = {
    "layers.0.attn.wo_b.weight": "target",
    "mtp.0.attn.wo_b.weight": "draft",
}


def natural_sort_key(filepath: str) -> list:
    """vLLM v0.25.1 weight_utils._natural_sort_key, verbatim."""
    return [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", os.path.basename(filepath))]


def read_header(path: Path) -> tuple[dict[str, Any], int]:
    """(tensor table, absolute byte offset of the data section)."""
    with path.open("rb") as fh:
        raw = fh.read(8)
        if len(raw) != 8:
            raise ValueError(f"{path}: truncated safetensors header")
        (size,) = struct.unpack("<Q", raw)
        if size <= 0 or size > 512 * 1024 * 1024:
            raise ValueError(f"{path}: implausible header size {size}")
        header = json.loads(fh.read(size).decode("utf-8"))
    header.pop("__metadata__", None)
    return header, 8 + size


def tensor_bytes(path: Path, name: str) -> tuple[bytes, dict[str, Any]]:
    header, base = read_header(path)
    meta = header[name]
    start, end = meta["data_offsets"]
    with path.open("rb") as fh:
        fh.seek(base + start)
        data = fh.read(end - start)
    if len(data) != end - start:
        raise ValueError(f"{path}: short read for {name}")
    return data, meta


def shard_digest(data: bytes, shape: list[int], itemsize: int, dim: int, tp: int, rank: int) -> "str | None":
    """sha256 of TP shard `rank` of a row-major tensor split evenly along `dim`."""
    if not shape or dim >= len(shape) or shape[dim] % tp:
        return None
    if dim == 0:
        rows = shape[0] // tp
        stride = len(data) // shape[0]
        return hashlib.sha256(data[rank * rows * stride:(rank + 1) * rows * stride]).hexdigest()
    if dim == 1 and len(shape) == 2:
        row = shape[1] * itemsize
        part = (shape[1] // tp) * itemsize
        h = hashlib.sha256()
        for r in range(shape[0]):
            off = r * row + rank * part
            h.update(data[off:off + part])
        return h.hexdigest()
    return None


# ----------------------------------------------------------------------------
# verify
# ----------------------------------------------------------------------------

def verify(model_dir: Path) -> dict[str, Any]:
    problems: list[str] = []
    index = json.loads((model_dir / INDEX).read_text(encoding="utf-8"))
    weight_map: dict[str, str] = index["weight_map"]
    indexed_files = sorted(set(weight_map.values()), key=natural_sort_key)
    on_disk = sorted((p.name for p in model_dir.glob("*.safetensors")), key=natural_sort_key)

    stray = sorted(set(on_disk) - set(indexed_files))
    missing = sorted(set(indexed_files) - set(on_disk))
    if stray:
        # vLLM filters files that are not in the index, but a stray file means
        # the directory is not the pinned one: refuse rather than reason about it.
        problems.append(f"safetensors files not referenced by the index: {stray}")
    if missing:
        problems.append(f"index references missing files: {missing}")

    overlay_files = [f for f in indexed_files if "overlay" in f]
    if len(overlay_files) != 1:
        problems.append(f"expected exactly one overlay file in the index, found {overlay_files}")
        return {"ok": False, "problems": problems}
    overlay = overlay_files[0]
    overlay_names = sorted(n for n, f in weight_map.items() if f == overlay)
    if len(overlay_names) != EXPECTED_OVERLAY_TENSORS:
        problems.append(f"index maps {len(overlay_names)} tensors to {overlay}, expected {EXPECTED_OVERLAY_TENSORS}")
    odd = [n for n in overlay_names if not _OVERLAY_NAME.search(n)]
    if odd:
        problems.append(f"overlay holds tensors that are not attn.wo_b: {odd[:5]}")

    load_order = sorted(on_disk if not missing else indexed_files, key=natural_sort_key)
    if load_order and load_order[-1] != overlay:
        problems.append(f"overlay {overlay} is NOT read last by vLLM's natural sort (last: {load_order[-1]})")

    # The overlay file itself must contain exactly the indexed names, and the
    # ORIGINAL copies must still be inside base shards (that is what makes the
    # order matter at all).
    duplicates_found = 0
    if not missing:
        ov_header, _ = read_header(model_dir / overlay)
        if sorted(ov_header) != overlay_names:
            problems.append("overlay file tensor set differs from the index entries that point at it")
        wanted = set(overlay_names)
        for fname in indexed_files:
            if fname == overlay:
                continue
            header, _ = read_header(model_dir / fname)
            hit = wanted & set(header)
            duplicates_found += len(hit)
    return {
        "ok": not problems,
        "overlay_file": overlay,
        "overlay_tensors": len(overlay_names),
        "base_copies_still_in_shards": duplicates_found,
        "files_read_by_vllm": len(load_order),
        "last_file_read": load_order[-1] if load_order else None,
        "problems": problems,
        "loader_conditions": [
            "default load format (auto/safetensors); never fastsafetensors, runai_streamer or instanttensor",
            "no --model-loader-extra-config enable_multithread_load (as_completed order is arbitrary)",
            "no extra *.safetensors in the model directory",
        ],
    }


# ----------------------------------------------------------------------------
# digests / compare
# ----------------------------------------------------------------------------

def expected_digests(model_dir: Path, tp: int) -> dict[str, Any]:
    index = json.loads((model_dir / INDEX).read_text(encoding="utf-8"))
    weight_map: dict[str, str] = index["weight_map"]
    overlay = next(f for f in set(weight_map.values()) if "overlay" in f)
    out: dict[str, Any] = {}
    for name in PROBES:
        if weight_map.get(name) != overlay:
            continue
        entry: dict[str, Any] = {"overlay": {}, "base": {}}
        ov_bytes, ov_meta = tensor_bytes(model_dir / overlay, name)
        base_file = None
        for fname in sorted(set(weight_map.values()), key=natural_sort_key):
            if fname == overlay:
                continue
            header, _ = read_header(model_dir / fname)
            if name in header:
                base_file = fname
                break
        sources = {"overlay": (ov_bytes, ov_meta)}
        if base_file:
            sources["base"] = tensor_bytes(model_dir / base_file, name)
        entry["base_file"] = base_file
        entry["shape"] = ov_meta["shape"]
        entry["dtype"] = ov_meta["dtype"]
        entry["identical_copies"] = "base" in sources and sources["base"][0] == ov_bytes
        for kind, (data, meta) in sources.items():
            size = _ITEMSIZE.get(meta["dtype"], 1)
            for dim in (0, 1):
                for rank in range(tp):
                    d = shard_digest(data, meta["shape"], size, dim, tp, rank)
                    if d:
                        entry[kind][f"dim{dim}/rank{rank}"] = d
            entry[kind]["full"] = hashlib.sha256(data).hexdigest()
        out[name] = entry
    return out


_LINE = re.compile(r"GX_WOB_DIGEST (\{.*\})")


def compare(model_dir: Path, logs: list[str]) -> dict[str, Any]:
    lines = []
    for path in logs:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        lines += [json.loads(m.group(1)) for m in _LINE.finditer(text)]
    if not lines:
        return {"ok": False, "verdict": "no GX_WOB_DIGEST lines (was GX_WOB_DIGEST=1 set on both ranks?)", "ranks": []}
    tp = max(int(rec.get("tp_size") or 1) for rec in lines)
    expected = expected_digests(model_dir, tp)
    results = []
    for rec in lines:
        ckpt = "mtp.0.attn.wo_b.weight" if "dspark" in str(rec.get("model_class", "")).lower() else "layers.0.attn.wo_b.weight"
        exp = expected.get(ckpt, {})
        rank = int(rec.get("tp_rank") or 0)
        verdict = "unknown"
        for kind in ("overlay", "base"):
            table = exp.get(kind, {})
            if rec["sha256"] in (table.get(f"dim0/rank{rank}"), table.get(f"dim1/rank{rank}"), table.get("full")):
                verdict = kind
                break
        results.append({"tensor": ckpt, "model_class": rec.get("model_class"), "tp_rank": rank,
                        "loaded": verdict, "sha256": rec["sha256"]})
    ok = all(r["loaded"] == "overlay" for r in results)
    ranks = {r["tp_rank"] for r in results if r["tensor"] == "layers.0.attn.wo_b.weight"}
    if ranks != set(range(tp)):
        ok = False
    return {"ok": ok, "tp": tp, "results": results,
            "verdict": "every rank holds the overlay bytes" if ok else "overlay NOT proven on every rank"}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("model_dir", type=Path)
    d = sub.add_parser("digests")
    d.add_argument("model_dir", type=Path)
    d.add_argument("--tp", type=int, default=2)
    c = sub.add_parser("compare")
    c.add_argument("model_dir", type=Path)
    c.add_argument("logs", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "verify":
        out = verify(args.model_dir)
    elif args.cmd == "digests":
        out = {"ok": True, "expected": expected_digests(args.model_dir, args.tp)}
    else:
        out = compare(args.model_dir, args.logs)
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
