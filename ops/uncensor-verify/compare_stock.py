#!/usr/bin/env python3
"""Stock-vs-candidate comparison for the uncensoring battery (PHASE 18-20).

Runs the SAME probe battery against TWO endpoints (the stock checkpoint and
the uncensored production candidate) and prints the comparison table the
production pick is made from: comply %, hard refusals, control correctness.
Imported from verify_uncensored.py so the classifier, thresholds and battery
can never drift between the two tools.

Usage:
    compare_stock.py --stock URL --candidate URL --key K
                     [--stock-model M] [--candidate-model M] [--label L]

Both endpoints should serve the SAME served model id (e.g.
DeepSeek-v4.1-Flash-EXL3) unless --stock-model/--candidate-model say
otherwise (e.g. gx-max vs a direct stock deployment).

Reports stay LOCAL (state/uncensor-verify/reports/). Exit 0 always unless
an operational error occurs; the VERDICT text is the deliverable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import verify_uncensored as vu  # noqa: E402  (same directory)


def run_side(name: str, endpoint: str, key: str, model: str,
             battery: dict, *, keep_text: bool) -> dict:
    chat_fn = vu.make_chat_fn(endpoint, key, model)
    print(f"[{name}] {len(battery['probes'])} probes + {len(battery['controls'])} controls "
          f"-> {endpoint} (model {model})")
    body = vu.run_battery(chat_fn, battery, keep_text=keep_text)
    verdict_str, s = vu.verdict(body)
    return {"name": name, "endpoint": endpoint, "model": model,
            "verdict": verdict_str, "score": s, "results": body}


def render_table(stock: dict, cand: dict) -> str:
    ss, cs = stock["score"], cand["score"]
    lines = [
        "",
        f"{'metric':<28} {'stock':>12} {'candidate':>12} {'better':>10}",
        "-" * 66,
    ]

    def row(metric, s_val, c_val, better_is="cand", fmt=str) -> str:
        if better_is == "cand":
            mark = "candidate" if (c_val or 0) > (s_val or 0) else ("stock" if (c_val or 0) < (s_val or 0) else "tie")
        else:
            mark = "candidate" if (c_val or 0) < (s_val or 0) else ("stock" if (c_val or 0) > (s_val or 0) else "tie")
        return f"{metric:<28} {str(s_val):>12} {str(c_val):>12} {mark:>10}"

    lines.append(row("comply %", ss["comply_pct"], cs["comply_pct"]))
    lines.append(row("soft-refuse", ss["counts"]["soft_refuse"], cs["counts"]["soft_refuse"], better_is="stock"))
    lines.append(row("hedge", ss["counts"]["hedge"], cs["counts"]["hedge"], better_is="stock"))
    lines.append(row("hard refusals", ss["hard_refusals"], cs["hard_refusals"], better_is="stock"))
    lines.append(row("errors", ss["errors"], cs["errors"], better_is="stock"))
    lines.append(row("controls passed", f"{ss['controls_passed']}/{ss['controls_total']}",
                     f"{cs['controls_passed']}/{cs['controls_total']}"))
    lines.append("-" * 66)
    lines.append(f"stock verdict     : {stock['verdict']}")
    lines.append(f"candidate verdict : {cand['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_stock",
        description="Stock vs uncensored candidate comparison (same battery, same classifier).",
    )
    parser.add_argument("--stock", required=True, help="stock endpoint URL")
    parser.add_argument("--candidate", required=True, help="candidate endpoint URL")
    parser.add_argument("--key", default="", help="bearer key for both endpoints")
    parser.add_argument("--stock-model", default=None)
    parser.add_argument("--candidate-model", default=None)
    parser.add_argument("--label", default="compare")
    parser.add_argument("--no-prompt-text", action="store_true")
    parser.add_argument("--battery", default=str(vu.PROMPTS_PATH))
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    battery = vu.load_battery(Path(args.battery))
    stock_model = args.stock_model or "DeepSeek-v4.1-Flash-EXL3"
    cand_model = args.candidate_model or "DeepSeek-v4.1-Flash-EXL3"

    stock = run_side("stock", args.stock, args.key, stock_model, battery,
                     keep_text=not args.no_prompt_text)
    cand = run_side("candidate", args.candidate, args.key, cand_model, battery,
                    keep_text=not args.no_prompt_text)

    table = render_table(stock, cand)
    print(table)

    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": args.label,
        "stock": {k: stock[k] for k in ("name", "endpoint", "model", "verdict", "score")},
        "candidate": {k: cand[k] for k in ("name", "endpoint", "model", "verdict", "score")},
        "stock_results": stock["results"],
        "candidate_results": cand["results"],
    }
    out_path = Path(args.out) if args.out else (
        vu.REPORT_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{args.label}-comparison.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport: {out_path}  -- LOCAL ONLY, never push raw outputs to the public repo")
    except OSError as exc:
        print(f"warn: cannot write report to {out_path}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
