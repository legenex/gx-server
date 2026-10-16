#!/usr/bin/env python3
"""Pass/fail evaluation for accept.sh (stdlib only, unit tested).

    accept_eval.py mem   MEM_TSV [--floor 15] [--min-samples 10]
    accept_eval.py bench BENCH_LOG
    accept_eval.py ranks BEFORE AFTER

Each prints one line and exits 0 (PASS) or 1 (FAIL). Missing or malformed
input is a FAIL with an explicit reason, never a crash.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def eval_mem(text: str, floor: float = 15.0, min_samples: int = 10) -> tuple[bool, str]:
    """MemAvailable samples ("time<TAB>head<TAB>worker"): every reading present,
    enough of them, and both nodes at or above the floor the whole time."""
    rows = [line.split("\t") for line in text.splitlines() if line.strip()]
    if not rows:
        return False, "no memory samples at all (watcher did not run)"
    if any(len(r) != 3 for r in rows):
        return False, "malformed memory samples"
    na = sum(1 for r in rows for v in r[1:] if v == "NA")
    if na:
        return False, f"{na} reading(s) could not be taken (NA): memory not proven"
    try:
        heads = [float(r[1]) for r in rows]
        workers = [float(r[2]) for r in rows]
    except ValueError:
        return False, "non-numeric memory sample"
    if len(rows) < min_samples:
        return False, f"only {len(rows)} sample(s) per node (< {min_samples}): load window not covered"
    h, w = min(heads), min(workers)
    msg = f"min MemAvailable head={h:.1f} worker={w:.1f} GiB over {len(rows)} samples (floor {floor:g})"
    return (h >= floor and w >= floor), msg


def eval_bench(text: str) -> tuple[bool, str]:
    """gx_bench REPORT: every run complete, every request produced tokens and no
    raw think tags, and the tool call + continuation both worked."""
    reports = [line[len("REPORT "):] for line in text.splitlines() if line.startswith("REPORT ")]
    if not reports:
        return False, "no REPORT line (bench did not finish)"
    try:
        rep = json.loads(reports[-1])
    except ValueError:
        return False, "unparsable REPORT"
    problems = []
    for run in rep.get("runs") or []:
        n = run.get("concurrency")
        if run.get("errors"):
            problems.append(f"c={n}: {len(run['errors'])} error(s)")
        if run.get("ok") != n:
            problems.append(f"c={n}: {run.get('ok')} of {n} completed")
        for i, req in enumerate(run.get("requests") or []):
            if not (req.get("completion_tokens") or 0) > 0:
                problems.append(f"c={n} req {i}: completion_tokens 0")
            if req.get("think_tag_leak"):
                problems.append(f"c={n} req {i}: raw <think> tag in content")
    if not rep.get("runs"):
        problems.append("no runs")
    tools = rep.get("tools") or {}
    if not (tools.get("tool_call") and tools.get("continuation")):
        problems.append(f"tools: {tools}")
    if problems:
        return False, "; ".join(problems)[:500]
    agg = ", ".join(f"c={r['concurrency']} {r.get('aggregate_tok_s')} tok/s" for r in rep["runs"])
    return True, agg


def _parse_ranks(text: str) -> dict[str, tuple[str, str, str]]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            out[parts[0]] = (parts[1], parts[2], parts[3])
    return out


def eval_ranks(before: str, after: str) -> tuple[bool, str]:
    """"<node> <RestartCount> <StartedAt> <Running>" per rank, before and after
    the acceptance run: both running, nothing restarted in between."""
    b, a = _parse_ranks(before), _parse_ranks(after)
    want = ("node1", "node2")
    if any(n not in b or n not in a for n in want):
        return False, f"rank state missing (before {sorted(b)}, after {sorted(a)})"
    problems = []
    for n in want:
        if a[n][2] != "true" or b[n][2] != "true":
            problems.append(f"{n} not running")
        if a[n][0] != b[n][0]:
            problems.append(f"{n} RestartCount {b[n][0]} -> {a[n][0]}")
        if a[n][1] != b[n][1]:
            problems.append(f"{n} StartedAt changed ({b[n][1]} -> {a[n][1]})")
    if problems:
        return False, "; ".join(problems)
    return True, f"no restarts (node1 count {a['node1'][0]}, node2 count {a['node2'][0]})"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mem")
    m.add_argument("tsv")
    m.add_argument("--floor", type=float, default=15.0)
    m.add_argument("--min-samples", type=int, default=10)
    b = sub.add_parser("bench")
    b.add_argument("log")
    r = sub.add_parser("ranks")
    r.add_argument("before")
    r.add_argument("after")
    args = ap.parse_args(argv)

    def read(p: str) -> str:
        try:
            return Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if args.cmd == "mem":
        ok, msg = eval_mem(read(args.tsv), args.floor, args.min_samples)
    elif args.cmd == "bench":
        ok, msg = eval_bench(read(args.log))
    else:
        ok, msg = eval_ranks(read(args.before), read(args.after))
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
