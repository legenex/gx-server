#!/usr/bin/env python3
"""Real-inference acceptance for the gx text tiers, through the LiteLLM gateway.

    gx_tier_acceptance.py gx-mini [--checks text,math,tools,vision,stream,kilo,long,code,reason]
    gx_tier_acceptance.py gx-auto-kilo          # gx-auto routing on Kilo-shaped requests

Every check sends a real request and inspects the real answer. Nothing is
"passed" because an endpoint returned 200. Results (metrics, verdicts, the
first 400 characters of each answer) are written to
/srv/logs/acceptance/<UTC>-<alias>.json.

Credentials: GX_GATEWAY_KEY from the environment, or LITELLM_MASTER_KEY read
server-side from the ignored gateway .env. The key is never printed or
written anywhere.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "legenex" / "orchestrator"))
sys.path.insert(0, str(REPO / "legenex" / "orchestrator" / "tests"))

GATEWAY = os.environ.get("GX_GATEWAY_URL", "http://127.0.0.1:4000/v1")
ORCH = os.environ.get("GX_ORCH_URL", "http://127.0.0.1:18900")
OUT_DIR = Path(os.environ.get("GX_ACCEPTANCE_DIR", "/srv/logs/acceptance"))


def gateway_key() -> str:
    key = os.environ.get("GX_GATEWAY_KEY", "").strip()
    if key:
        return key
    env = REPO / "legenex" / "gateway" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no gateway key available")


KEY = ""


def _req(path: str, body: dict | None = None, *, timeout: float = 1800, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GATEWAY + path,
        data=data,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", **(headers or {})},
        method="POST" if body is not None else "GET",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def chat(model: str, messages: list, **kw) -> tuple[dict, float, dict]:
    t0 = time.monotonic()
    with _req("/chat/completions", {"model": model, "messages": messages, **kw}) as r:
        body = json.load(r)
        hdrs = dict(r.headers)
    return body, time.monotonic() - t0, hdrs


def stream_chat(model: str, messages: list, headers: dict | None = None, **kw) -> dict:
    """Streaming request; returns TTFT, total time, text, chunk count, tokens/s."""
    t0 = time.monotonic()
    first = None
    text = []
    reasoning = []
    chunks = 0
    usage = {}
    tool_names = []
    with _req(
        "/chat/completions",
        {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}, **kw},
        headers=headers,
    ) as r:
        resp_headers = dict(r.headers)
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                piece = d.get("content") or ""
                rpiece = d.get("reasoning_content") or d.get("reasoning") or ""
                for tc in d.get("tool_calls") or []:
                    name = (tc.get("function") or {}).get("name")
                    if name:
                        tool_names.append(name)
                if (piece or rpiece or d.get("tool_calls")) and first is None:
                    first = time.monotonic() - t0
                if piece:
                    text.append(piece)
                if rpiece:
                    reasoning.append(rpiece)
                chunks += 1
    total = time.monotonic() - t0
    completion = usage.get("completion_tokens") or 0
    gen_time = max(1e-6, total - (first or 0))
    return {
        "ttft_s": round(first, 3) if first is not None else None,
        "total_s": round(total, 3),
        "chunks": chunks,
        "text": "".join(text),
        "reasoning_chars": len("".join(reasoning)),
        "completion_tokens": completion,
        "prompt_tokens": usage.get("prompt_tokens"),
        "tokens_per_s": round(completion / gen_time, 1) if completion else None,
        "tool_calls": tool_names,
        "headers": {k: v for k, v in resp_headers.items() if k.lower().startswith("x-gx") or k.lower() == "x-litellm-model-id"},
    }


def vision_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("RGB", (400, 220), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((30, 60, 130, 160), fill=(220, 20, 20))
    d.rectangle((160, 60, 260, 160), fill=(20, 60, 220))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
    except OSError:
        font = ImageFont.load_default()
    d.text((290, 45), "7", fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
            "required": ["city"],
        },
    },
}


def run_checks(alias: str, checks: list[str], thinking: bool | None) -> list[dict]:
    results: list[dict] = []
    extra: dict = {}
    if thinking is not None:
        extra["chat_template_kwargs"] = {"enable_thinking": thinking}

    def record(name: str, ok: bool, **info):
        results.append({"check": name, "pass": bool(ok), **info})
        flag = "PASS" if ok else "FAIL"
        brief = {k: v for k, v in info.items() if k not in ("answer",)}
        print(f"[{flag}] {alias} {name} {json.dumps(brief)[:300]}", flush=True)

    def safe(name, fn):
        try:
            fn()
        except urllib.error.HTTPError as exc:
            record(name, False, error=f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}")
        except Exception as exc:  # noqa: BLE001
            record(name, False, error=repr(exc)[:300])

    if "warmup" in checks:
        def _w():
            s = stream_chat(alias, [{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=512, **extra)
            record("warmup", bool(s["text"] or s["reasoning_chars"]), total_s=s["total_s"], ttft_s=s["ttft_s"], answer=s["text"][:200])
        safe("warmup", _w)

    if "text" in checks:
        def _t():
            s = stream_chat(alias, [{"role": "user", "content": "What is the capital city of Australia? Answer in one word."}], max_tokens=2048, **extra)
            record("text", "canberra" in s["text"].lower(), ttft_s=s["ttft_s"], total_s=s["total_s"], answer=s["text"][:400])
        safe("text", _t)

    if "math" in checks:
        def _m():
            s = stream_chat(alias, [{"role": "user", "content": "Compute 347 * 29. Reply with only the number."}], max_tokens=4096, **extra)
            record("math", "10063" in s["text"].replace(",", ""), ttft_s=s["ttft_s"], answer=s["text"][:400])
        safe("math", _m)

    if "stream" in checks:
        def _s():
            s = stream_chat(alias, [{"role": "user", "content": "Write three short sentences about the ocean."}], max_tokens=1024, temperature=0.7, **extra)
            record("stream", s["chunks"] >= 3 and len(s["text"]) > 40, chunks=s["chunks"], ttft_s=s["ttft_s"],
                   tokens_per_s=s["tokens_per_s"], completion_tokens=s["completion_tokens"], answer=s["text"][:400])
        safe("stream", _s)

    if "throughput" in checks:
        def _tp():
            s = stream_chat(alias, [{"role": "user", "content": "Write a 400-word explanation of how TCP congestion control works."}],
                            max_tokens=1200, temperature=0.3, **extra)
            record("throughput", (s["tokens_per_s"] or 0) > 0 and s["completion_tokens"] > 200, ttft_s=s["ttft_s"],
                   tokens_per_s=s["tokens_per_s"], completion_tokens=s["completion_tokens"], total_s=s["total_s"])
        safe("throughput", _tp)

    if "tools" in checks:
        def _tl():
            body, dt, _ = chat(alias, [{"role": "user", "content": "What's the weather in Paris right now? Use the tool."}],
                               tools=[WEATHER_TOOL], tool_choice="auto", max_tokens=2048, **extra)
            msg = body["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            ok = bool(calls) and calls[0]["function"]["name"] == "get_weather" and "paris" in calls[0]["function"]["arguments"].lower()
            record("tools", ok, seconds=round(dt, 2), tool_calls=[c["function"] for c in calls][:2])
        safe("tools", _tl)

    if "vision" in checks:
        def _v():
            b64 = base64.b64encode(vision_png()).decode()
            s = stream_chat(alias, [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image: list each shape with its colour, and any digit you see."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], max_tokens=2048, **extra)
            t = s["text"].lower()
            ok = all(w in t for w in ("red", "blue", "circle", "square")) and ("7" in t or "seven" in t)
            record("vision", ok, ttft_s=s["ttft_s"], answer=s["text"][:400])
        safe("vision", _v)

    if "code" in checks:
        def _c():
            s = stream_chat(alias, [{"role": "user", "content":
                "Write a Python function `is_prime(n: int) -> bool`. Reply with only one ```python code block, no prose."}],
                max_tokens=4096, temperature=0, **extra)
            m = re.search(r"```(?:python)?\n(.*?)```", s["text"], re.S)
            ok = False
            detail = "no code block"
            if m:
                ns: dict = {}
                try:
                    exec(compile(m.group(1), "<model>", "exec"), ns)  # noqa: S102 - acceptance of model-written code, sandboxless by design: tiny pure function
                    f = ns["is_prime"]
                    got = [n for n in range(-3, 60) if f(n)]
                    ok = got == [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59]
                    detail = f"primes<60 -> {got}"
                except Exception as exc:  # noqa: BLE001
                    detail = f"exec failed: {exc!r}"
            record("code", ok, detail=detail[:200], ttft_s=s["ttft_s"], tokens_per_s=s["tokens_per_s"])
        safe("code", _c)

    if "reason" in checks:
        def _r():
            prompt = ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
                      "Then a second puzzle: how many positive integers n <= 1000 are divisible by 3 or 5 but not by 15? "
                      "Give both answers on the final line in the form `ANSWERS: <ball price>, <count>`.")
            s = stream_chat(alias, [{"role": "user", "content": prompt}], max_tokens=16000, temperature=0, **extra)
            t = s["text"].replace(" ", "")
            # multiples of 3: 333, of 5: 200, of 15: 66 -> (333+200-66) - 66 = 401
            ok = ("0.05" in t) and ("401" in t)
            record("reason", ok, ttft_s=s["ttft_s"], total_s=s["total_s"], reasoning_chars=s["reasoning_chars"],
                   completion_tokens=s["completion_tokens"], tokens_per_s=s["tokens_per_s"], answer=s["text"][-300:])
        safe("reason", _r)

    if "codereason" in checks:
        def _cr():
            prompt = (
                "What does this Python print? Think carefully, then give the exact output on the final line as `OUTPUT: ...`.\n\n"
                "```python\n"
                "def f(xs, acc=[]):\n"
                "    for x in xs:\n"
                "        if x % 2:\n"
                "            acc.append(x * x)\n"
                "    return acc\n"
                "a = f([1, 2, 3])\n"
                "b = f([5])\n"
                "print(len(a), sum(b), a is b)\n"
                "```"
            )
            s = stream_chat(alias, [{"role": "user", "content": prompt}], max_tokens=16000, temperature=0, **extra)
            ok = bool(re.search(r"OUTPUT:\s*`?3\s+35\s+True", s["text"]))
            record("codereason", ok, ttft_s=s["ttft_s"], reasoning_chars=s["reasoning_chars"], answer=s["text"][-200:])
        safe("codereason", _cr)

    if "long" in checks:
        def _l():
            s = stream_chat(alias, [{"role": "user", "content":
                "Write a detailed technical design document (at least 1500 words) for a rate limiter service, with sections."}],
                max_tokens=6000, temperature=0.5, **extra)
            words = len(s["text"].split())
            record("long", words >= 900, words=words, completion_tokens=s["completion_tokens"],
                   tokens_per_s=s["tokens_per_s"], total_s=s["total_s"])
        safe("long", _l)

    if "kilo" in checks:
        def _k():
            from kilo_fixtures import kilo_request

            req = kilo_request("are you there?")
            req.pop("model")
            req.pop("stream")
            s = stream_chat(alias, req["messages"], tools=req["tools"], tool_choice="auto", max_tokens=4096, **extra)
            answered = bool(s["text"].strip()) or bool(s["tool_calls"])
            record("kilo", answered, ttft_s=s["ttft_s"], total_s=s["total_s"], prompt_tokens=s["prompt_tokens"],
                   tool_calls=s["tool_calls"][:3], answer=s["text"][:300])
        safe("kilo", _k)

    return results


def run_auto_kilo() -> list[dict]:
    """Route every Kilo fixture through gx-auto and match the router's own decision by request id."""
    from gx_orchestrator.classifier import request_fingerprint
    from kilo_fixtures import KILO_ROUTING_CASES, kilo_request

    results = []
    for idx, (task, expected, why) in enumerate(KILO_ROUTING_CASES):
        rid = f"acc-{int(time.time())}-{idx}"
        req = kilo_request(task, max_tokens=4096)
        req.pop("model")
        req.pop("stream")
        fp = request_fingerprint({"messages": req["messages"]})
        try:
            s = stream_chat("gx-auto", req["messages"], headers={"X-GX-Request-Id": rid},
                            tools=req["tools"], tool_choice="auto", max_tokens=req["max_tokens"], temperature=0)
            err = None
        except urllib.error.HTTPError as exc:
            s, err = {}, f"HTTP {exc.code}"
        # The gateway may not forward our request-id header; the messages
        # fingerprint identifies this exact request either way.
        with urllib.request.urlopen(f"{ORCH}/routing/decisions?fingerprint={fp}&limit=10", timeout=10) as r:
            recs = json.load(r)["data"]
        decisions = [x for x in recs if x.get("event") == "decision"]
        completed = [x for x in recs if x.get("event") == "completed"]
        decision = decisions[0] if decisions else {}
        done = next((c for c in completed if c.get("request_id") == decision.get("request_id")), {})
        ok = decision.get("tier") == expected and err is None and bool(s.get("text", "").strip() or s.get("tool_calls"))
        row = {
            "check": f"auto:{why}", "pass": ok, "task": task[:80], "expected": expected,
            "router_tier": decision.get("tier"), "router_request_id": decision.get("request_id"),
            "fingerprint": fp, "intent": decision.get("intent"), "tool_schema_tokens": decision.get("tool_schema_tokens"),
            "prompt_tokens_est": decision.get("prompt_tokens"), "router_elapsed_ms": done.get("elapsed_ms"),
            "ttft_s": s.get("ttft_s"), "total_s": s.get("total_s"), "error": err,
            "answer": (s.get("text") or "")[:160],
        }
        results.append(row)
        print(f"[{'PASS' if ok else 'FAIL'}] {row['check']}: router={row['router_tier']} expected={expected} "
              f"ttft={row['ttft_s']} total={row['total_s']} rid={row['router_request_id']}", flush=True)
    return results


def main() -> int:
    global KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("alias")
    ap.add_argument("--checks", default="warmup,text,math,stream,throughput,tools,vision,code,kilo")
    ap.add_argument("--thinking", choices=["on", "off"], default=None)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    KEY = gateway_key()
    started = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if args.alias == "gx-auto-kilo":
        results = run_auto_kilo()
    else:
        thinking = None if args.thinking is None else args.thinking == "on"
        results = run_checks(args.alias, [c.strip() for c in args.checks.split(",") if c.strip()], thinking)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{started}-{args.alias}{('-' + args.label) if args.label else ''}.json"
    out.write_text(json.dumps({"alias": args.alias, "started": started, "results": results}, indent=2))
    passed = sum(r["pass"] for r in results)
    print(f"SUMMARY {args.alias}: {passed}/{len(results)} passed -> {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
