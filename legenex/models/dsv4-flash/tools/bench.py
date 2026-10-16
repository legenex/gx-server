# Copied from the lead's V4.1 baseline script (2026-10-16) so the V4 Flash acceptance
# run in the activation window uses the identical method. Key: $GX_BENCH_KEY (never printed).
"""GX inference benchmark: TTFT, per-request decode tok/s, aggregate tok/s, tool calls.

Usage: python3 gx_bench.py --model gx-max --concurrency 1,2 [--extra '{"chat_template_kwargs":{...}}']
Reads the bearer key from $GX_BENCH_KEY. Token counts come from usage.completion_tokens,
never from counting SSE chunks (speculative decoding emits several tokens per chunk).
"""
import argparse
import json
import os
import threading
import time
import urllib.request

PROMPT = ("Write a Python function that parses an ISO-8601 duration string such as "
          "'P3DT4H12M' into total seconds, with input validation and five doctest examples. "
          "Explain the edge cases briefly after the code.")
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_service_health",
        "description": "Return the health of a named platform service.",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}},
                       "required": ["service"]},
    },
}]


EXTRA_HEADERS = {}


def post(url, key, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", **EXTRA_HEADERS})
    return urllib.request.urlopen(req, timeout=timeout)


def stream_once(url, key, model, extra, max_tokens, timeout):
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": max_tokens, "messages": [{"role": "user", "content": PROMPT}], **extra}
    start = time.monotonic()
    first = None
    usage, content_chars, reasoning_chars, leaked = None, 0, 0, False
    with post(url, key, body, timeout) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if (text or reasoning) and first is None:
                    first = time.monotonic()
                content_chars += len(text)
                reasoning_chars += len(reasoning)
                leaked |= "<think>" in text or "</think>" in text
    end = time.monotonic()
    tokens = (usage or {}).get("completion_tokens", 0)
    decode = tokens / (end - first) if first and end > first else 0.0
    return {"ttft_s": round(first - start, 2) if first else None, "total_s": round(end - start, 2),
            "completion_tokens": tokens, "decode_tok_s": round(decode, 2),
            "content_chars": content_chars, "reasoning_chars": reasoning_chars, "think_tag_leak": leaked}


def tool_check(url, key, model, extra, timeout):
    msgs = [{"role": "user", "content": "Check the health of the service named librechat using the tool."}]
    body = {"model": model, "messages": msgs, "tools": TOOLS, "tool_choice": "auto", "max_tokens": 2048, **extra}
    with post(url, key, body, timeout) as resp:
        first = json.load(resp)["choices"][0]["message"]
    calls = first.get("tool_calls") or []
    ok_call = bool(calls) and calls[0]["function"]["name"] == "get_service_health" \
        and json.loads(calls[0]["function"]["arguments"] or "{}").get("service") == "librechat"
    if not ok_call:
        return {"tool_call": False, "continuation": False, "raw": str(first)[:300]}
    msgs += [{"role": "assistant", "content": first.get("content") or "", "tool_calls": calls},
             {"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps({"service": "librechat", "status": "healthy", "uptime_h": 41})}]
    body["messages"] = msgs
    with post(url, key, body, timeout) as resp:
        final = json.load(resp)["choices"][0]["message"].get("content") or ""
    return {"tool_call": True, "continuation": "healthy" in final.lower(), "final_excerpt": final[:160]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4000/v1/chat/completions")
    ap.add_argument("--model", required=True)
    ap.add_argument("--concurrency", default="1,2")
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--extra", default="{}")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--skip-tools", action="store_true")
    ap.add_argument("--header", action="append", default=[], help="extra request header Name:Value")
    args = ap.parse_args()
    key = os.environ["GX_BENCH_KEY"]
    EXTRA_HEADERS.update(h.split(":", 1) for h in args.header)
    extra = json.loads(args.extra)
    report = {"model": args.model, "extra": extra, "started": time.strftime("%FT%T%z"), "runs": []}
    for n in [int(x) for x in args.concurrency.split(",")]:
        results, errors = [], []

        def worker():
            try:
                results.append(stream_once(args.url, key, args.model, extra, args.max_tokens, args.timeout))
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
        t0 = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.monotonic() - t0
        agg = sum(r["completion_tokens"] for r in results) / wall if wall else 0
        report["runs"].append({"concurrency": n, "wall_s": round(wall, 2), "aggregate_tok_s": round(agg, 2),
                               "ok": len(results), "errors": errors, "requests": results})
        print(json.dumps(report["runs"][-1]), flush=True)
    if not args.skip_tools:
        try:
            report["tools"] = tool_check(args.url, key, args.model, extra, args.timeout)
        except Exception as exc:
            report["tools"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        print(json.dumps(report["tools"]), flush=True)
    print("REPORT " + json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
