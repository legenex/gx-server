#!/usr/bin/env python3
"""Unified-memory budget for the dsv4-flash ranks (GB10: GPU memory IS host RAM).

vLLM on GB10 sizes its pool as ``gpu_memory_utilization x total`` where total
is the node's MemTotal (cudaMemGetInfo reports system RAM). Everything vLLM
keeps (weights, KV cache, activation workspace, CUDA graphs) lives inside that
pool; CUDA context, NCCL buffers and process overhead sit just outside it.

A node may start a rank only if, after the pool and that overhead are taken,
it still has the resident floor (15 GiB) of MemAvailable left:

    MemAvailable_now - util * MemTotal - overhead >= floor

    mem_budget.py check --util 0.74 --avail-gib 111.7 --total-gib 121.6
    mem_budget.py max-util --avail-gib 106.0 --total-gib 121.6
    mem_budget.py from-meminfo --util 0.74 [/proc/meminfo]

Exit 0 = fits, 1 = refuse. Output is one JSON object.
"""
from __future__ import annotations

import argparse
import json
import sys

DEFAULT_FLOOR_GIB = 15.0
DEFAULT_OVERHEAD_GIB = 4.0
#: Per-rank weight footprint of DeepSeek-V4-Flash-0731 at TP=2 (166.9 GB of
#: shards / 2, plus the replicated latent projections and the DSpark stages).
WEIGHTS_PER_RANK_GIB = 79.0


def need_gib(util: float, total_gib: float, floor: float = DEFAULT_FLOOR_GIB,
             overhead: float = DEFAULT_OVERHEAD_GIB) -> float:
    return util * total_gib + overhead + floor


def max_util(avail_gib: float, total_gib: float, floor: float = DEFAULT_FLOOR_GIB,
             overhead: float = DEFAULT_OVERHEAD_GIB) -> float:
    if total_gib <= 0:
        return 0.0
    return max(0.0, (avail_gib - floor - overhead) / total_gib)


def check(util: float, avail_gib: float, total_gib: float, floor: float = DEFAULT_FLOOR_GIB,
          overhead: float = DEFAULT_OVERHEAD_GIB) -> dict:
    need = need_gib(util, total_gib, floor, overhead)
    pool = util * total_gib
    after = avail_gib - pool - overhead
    out = {
        "util": util, "total_gib": round(total_gib, 1), "avail_gib": round(avail_gib, 1),
        "pool_gib": round(pool, 1), "overhead_gib": overhead, "floor_gib": floor,
        "need_gib": round(need, 1), "projected_after_load_gib": round(after, 1),
        "max_util_for_floor": round(max_util(avail_gib, total_gib, floor, overhead), 3),
        "kv_and_workspace_gib": round(pool - WEIGHTS_PER_RANK_GIB, 1),
        "fits": after >= floor,
    }
    if pool - WEIGHTS_PER_RANK_GIB < 4.0:
        out["fits"] = False
        out["reason"] = (f"pool {pool:.1f} GiB leaves under 4 GiB beyond the ~{WEIGHTS_PER_RANK_GIB} GiB "
                         "of weights per rank for KV cache and workspace")
    elif not out["fits"]:
        out["reason"] = (f"MemAvailable {avail_gib:.1f} GiB - pool {pool:.1f} - overhead {overhead} "
                         f"= {after:.1f} GiB < floor {floor} GiB; free {need - avail_gib:.1f} GiB first")
    return out


def read_meminfo(path: str = "/proc/meminfo") -> tuple[float, float]:
    vals = {}
    with open(path, encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                vals[key] = int(rest.split()[0]) / 1048576.0
    return vals["MemAvailable"], vals["MemTotal"]


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="dsv4-flash unified-memory budget")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "max-util", "from-meminfo"):
        sp = sub.add_parser(name)
        sp.add_argument("--floor-gib", type=float, default=DEFAULT_FLOOR_GIB)
        sp.add_argument("--overhead-gib", type=float, default=DEFAULT_OVERHEAD_GIB)
        if name != "max-util":
            sp.add_argument("--util", type=float, required=True)
        if name == "from-meminfo":
            sp.add_argument("meminfo", nargs="?", default="/proc/meminfo")
        else:
            sp.add_argument("--avail-gib", type=float, required=True)
            sp.add_argument("--total-gib", type=float, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "max-util":
        out = {"max_util_for_floor": round(max_util(args.avail_gib, args.total_gib,
                                                    args.floor_gib, args.overhead_gib), 3)}
        print(json.dumps(out))
        return 0
    if args.cmd == "from-meminfo":
        avail, total = read_meminfo(args.meminfo)
    else:
        avail, total = args.avail_gib, args.total_gib
    if not 0.05 <= args.util <= 0.95:
        print(json.dumps({"fits": False, "reason": f"util {args.util} outside 0.05..0.95"}))
        return 1
    out = check(args.util, avail, total, args.floor_gib, args.overhead_gib)
    print(json.dumps(out))
    return 0 if out["fits"] else 1


if __name__ == "__main__":
    sys.exit(main())
