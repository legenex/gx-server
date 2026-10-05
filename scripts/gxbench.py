#!/usr/bin/env python3
"""Gentle, reusable streaming benchmark for the GX cluster (gx-max / gx-auto via LiteLLM).

Stdlib only. Measures TTFT (first content/reasoning delta), output tok/s (decode, after
first token), and total latency for streaming chat completions; samples memory / GPU /
CPU on both nodes plus vLLM queue metrics before/during/after; snapshots RDMA error
counters before/after.

Key: read from env var named by --key-env (default GX10_GATEWAY_KEY). Never printed.
On gx10-01 the AI OS gateway key lives in ~/.config/ai-os/gx.env as GX_LITELLM_KEY:
  GX10_GATEWAY_KEY="$(grep -E '^GX_LITELLM_KEY=' ~/.config/ai-os/gx.env | cut -d= -f2-)" python3 bench.py
(the GX_GATEWAY_KEY in gx-cluster secrets/gateway.env is a stale virtual key limited to
 retired aliases gx-mini/gx-code* and gets 403 on gx-max.)
Example (identical re-run):
  GX10_GATEWAY_KEY=... python3 bench.py --model gx-max --out results.json
  python3 bench.py --only short --reps 1          # quick smoke

Defaults (documented, keep identical for A/B):
  * headers: X-GX-Reasoning: none (thinking OFF via chat_template_kwargs enable_thinking=false,
    applied by the orchestrator for both gx-max and gx-auto), X-GX-Priority: background
    (yield to real users; queue wait reported separately), X-GX-Project: gxbench.
    NEVER send X-GX-Profile with gx-max: a profile change restarts the engine (~9 min).
  * temperature 0, max_tokens 256, stream_options.include_usage = true.
  * every prompt starts with a random nonce so the prefix cache cannot hit across reps.
"""
from __future__ import annotations

import argparse, http.client, json, os, random, re, statistics, string, subprocess, sys, threading, time
from urllib.parse import urlparse

FILLER = (
    "The distributed inference cluster consists of two compact workstations linked by a "
    "high speed RDMA fabric. Each node holds half of the model weights and exchanges partial "
    "results after every layer. Engineers monitor latency, throughput, memory pressure and "
    "network counters to decide whether configuration changes are worth the restart cost. "
    "Historical notes describe warehouses, rivers, mountain trails, recipes for bread, the "
    "migration of birds, the rules of chess, and the slow evolution of programming languages. "
)
STORY = ("Write a long, detailed story (at least 600 words) about a lighthouse keeper who "
         "discovers an old radio. Do not stop early.")


def make_prompt(approx_tokens: int, rng: random.Random) -> str:
    nonce = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(24))
    head = f"[run-nonce {nonce}]\n"
    if approx_tokens <= 80:
        return head + STORY
    # ~5.6 chars/token for this filler on the DeepSeek V4.1 tokenizer (calibrated 2026-10-05: 4.3 gave 1666/12268 tok for 2k/16k)
    target_chars = int(approx_tokens * 5.6)
    body = []
    i = 0
    while sum(len(b) for b in body) < target_chars:
        body.append(f"Section {i}: " + FILLER)
        i += 1
    return head + "".join(body) + "\n\nIgnore the notes above. " + STORY


def stream_one(base: str, key: str, model: str, prompt: str, max_tokens: int, headers: dict,
               temperature: float, timeout: float) -> dict:
    u = urlparse(base)
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
        u.hostname, u.port, timeout=timeout)
    body = {
        "model": model, "stream": True, "max_tokens": max_tokens, "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "stream_options": {"include_usage": True},
    }
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **headers}
    t0 = time.perf_counter()
    rec: dict = {"ok": False}
    try:
        conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body), h)
        resp = conn.getresponse()
        rec["status"] = resp.status
        rec["queue_wait_ms"] = resp.getheader("x-gx-queue-wait-ms")
        rec["request_id"] = resp.getheader("x-gx-request-id") or resp.getheader("x-litellm-call-id")
        if resp.status != 200:
            rec["error"] = resp.read()[:400].decode("utf-8", "replace")
            return rec
        t_first = None; t_last = None; n_chunks = 0; usage = None; content_chars = 0; reasoning_chars = 0
        finish = None
        buf = b""
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                c = d.get("content") or ""
                r = d.get("reasoning_content") or d.get("reasoning") or ""
                if c or r:
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    t_last = now
                    n_chunks += 1
                    content_chars += len(c); reasoning_chars += len(r)
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        t_end = time.perf_counter()
        rec.update(ok=t_first is not None, total_s=t_end - t0,
                   ttft_s=(t_first - t0) if t_first else None,
                   chunks=n_chunks, content_chars=content_chars, reasoning_chars=reasoning_chars,
                   finish_reason=finish, usage=usage)
        ctoks = (usage or {}).get("completion_tokens")
        if t_first and t_last and ctoks and ctoks > 1 and t_last > t_first:
            rec["decode_tok_s"] = (ctoks - 1) / (t_last - t_first)
            rec["e2e_tok_s"] = ctoks / (t_end - t0)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["total_s"] = time.perf_counter() - t0
    finally:
        conn.close()
    return rec


# ---------------------------------------------------------------- system sampling
LOCAL_SAMPLE = r"""
m=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}/SwapTotal/{st=$2}/SwapFree/{sf=$2}END{printf "%.1f %.1f %.1f",(t-a)/1048576,a/1048576,(st-sf)/1048576}' /proc/meminfo)
g=$(nvidia-smi --query-gpu=utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
c=$(head -1 /proc/stat)
echo "$(date +%s.%N) $m $g $c"
"""


def _sampler(cmd_prefix: list, interval: float, out: list, stop: threading.Event, tag: str):
    loop = f"while true; do {LOCAL_SAMPLE.strip()}\nsleep {interval}; done"
    p = subprocess.Popen(cmd_prefix + [loop], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    def reader():
        for line in p.stdout:
            parts = line.split()
            try:
                ts = float(parts[0]); used, avail, swap = map(float, parts[1:4])
                gu, pw, tmp = (parts[4].split(",") + ["", "", ""])[:3]
                cpu = list(map(int, parts[6:14]))
                out.append({"node": tag, "ts": ts, "mem_used_gib": used, "mem_avail_gib": avail,
                            "swap_used_gib": swap, "gpu_util": float(gu) if gu else None,
                            "gpu_w": float(pw) if pw not in ("", "[N/A]") else None,
                            "cpu_jiffies": cpu})
            except Exception:
                pass
    th = threading.Thread(target=reader, daemon=True); th.start()
    stop.wait(); p.terminate()
    try: p.wait(timeout=5)
    except Exception: p.kill()


def cpu_util(samples: list) -> list:
    out = []
    for a, b in zip(samples, samples[1:]):
        ta, tb = sum(a["cpu_jiffies"]), sum(b["cpu_jiffies"])
        ia, ib = a["cpu_jiffies"][3] + a["cpu_jiffies"][4], b["cpu_jiffies"][3] + b["cpu_jiffies"][4]
        if tb > ta:
            out.append(100.0 * (1 - (ib - ia) / (tb - ta)))
    return out


def vllm_metrics(url: str) -> dict:
    try:
        u = urlparse(url)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
        c.request("GET", "/metrics"); txt = c.getresponse().read().decode()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    want = {"vllm:num_requests_running": "running", "vllm:num_requests_waiting": "waiting",
            "vllm:kv_cache_usage_perc": "kv_usage", "vllm:num_preemptions_total": "preemptions",
            "vllm:spec_decode_num_draft_tokens_total": "spec_draft",
            "vllm:spec_decode_num_accepted_tokens_total": "spec_accepted"}
    res = {}
    for line in txt.splitlines():
        for k, n in want.items():
            if line.startswith(k + "{"):
                res[n] = float(line.rsplit(" ", 1)[1])
    return res


RDMA_CNT = ("out_of_sequence packet_seq_err local_ack_timeout_err out_of_buffer roce_adp_retrans "
            "req_transport_retries_exceeded rnr_nak_retry_err req_cqe_error resp_cqe_error")


def rdma_counters(ssh: list | None) -> dict:
    cmd = ("for d in rocep1s0f0 roceP2p1s0f0; do for c in " + RDMA_CNT + "; do "
           "echo $d $c $(cat /sys/class/infiniband/$d/ports/1/hw_counters/$c 2>/dev/null); done; "
           "echo $d xmit $(cat /sys/class/infiniband/$d/ports/1/counters/port_xmit_data); done")
    try:
        txt = subprocess.run((ssh or ["bash", "-c"]) + [cmd], capture_output=True, text=True, timeout=20).stdout
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    out = {}
    for line in txt.splitlines():
        p = line.split()
        if len(p) == 3:
            out[f"{p[0]}.{p[1]}"] = int(p[2]) if p[2].isdigit() else p[2]
    return out


def summarize(runs: list) -> dict:
    ok = [r for r in runs if r.get("ok")]
    def med(k):
        v = [r[k] for r in ok if r.get(k) is not None]
        return round(statistics.median(v), 3) if v else None
    return {"n": len(runs), "ok": len(ok), "ttft_s_med": med("ttft_s"), "decode_tok_s_med": med("decode_tok_s"),
            "total_s_med": med("total_s"), "e2e_tok_s_med": med("e2e_tok_s"),
            "prompt_tokens_med": statistics.median([r["usage"]["prompt_tokens"] for r in ok if r.get("usage")]) if any(r.get("usage") for r in ok) else None,
            "completion_tokens_med": statistics.median([r["usage"]["completion_tokens"] for r in ok if r.get("usage")]) if any(r.get("usage") for r in ok) else None,
            "queue_wait_ms": [r.get("queue_wait_ms") for r in runs]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:4000/v1")
    ap.add_argument("--model", default="gx-max")
    ap.add_argument("--key-env", default="GX10_GATEWAY_KEY")
    ap.add_argument("--reasoning", default="none", help="X-GX-Reasoning header value ('' to omit)")
    ap.add_argument("--priority", default="background")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--long-reps", type=int, default=2, help="reps for the 16k case")
    ap.add_argument("--only", default="", help="comma list: short,medium,long,c2,c4,c8")
    ap.add_argument("--c8", action="store_true", help="include concurrency 8 (only when idle)")
    ap.add_argument("--metrics-url", default="http://127.0.0.1:8888")
    ap.add_argument("--worker-ssh", default="gx10-02")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--seed", type=int, default=0, help="0 = time-based nonce seed")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json"))
    a = ap.parse_args()
    key = os.environ.get(a.key_env)
    if not key:
        sys.exit(f"env {a.key_env} not set")
    rng = random.Random(a.seed or time.time_ns())
    headers = {"X-GX-Project": "gxbench", "X-GX-Agent": "baseline-bench", "X-GX-Priority": a.priority}
    if a.reasoning:
        headers["X-GX-Reasoning"] = a.reasoning
    only = set(filter(None, a.only.split(",")))
    sel = lambda n: (not only) or n in only  # noqa: E731

    samples: list = []; stop = threading.Event()
    wssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", a.worker_ssh] if a.worker_ssh else None
    ths = [threading.Thread(target=_sampler, args=(["bash", "-c"], 2, samples, stop, "gx10-01"), daemon=True)]
    if wssh:
        ths.append(threading.Thread(target=_sampler, args=(wssh, 2, samples, stop, "gx10-02"), daemon=True))
    for t in ths: t.start()
    result = {"config": {k: v for k, v in vars(a).items()}, "headers": headers,
              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phases": [], "cases": {}}
    result["rdma_before"] = {"gx10-01": rdma_counters(None), "gx10-02": rdma_counters(wssh) if wssh else None}

    def phase(name):
        result["phases"].append({"name": name, "ts": time.time(), "vllm": vllm_metrics(a.metrics_url)})
        print(f"[{time.strftime('%H:%M:%S')}] {name} {result['phases'][-1]['vllm']}", flush=True)

    time.sleep(6); phase("before")

    def seq_case(name, approx, reps):
        runs = []
        for i in range(reps):
            m0 = vllm_metrics(a.metrics_url)
            r = stream_one(a.base, key, a.model, make_prompt(approx, rng), a.max_tokens, headers, a.temperature, a.timeout)
            r["vllm_at_start"] = m0; r["t_start"] = time.time()
            runs.append(r)
            print(f"  {name}#{i}: ttft={r.get('ttft_s')} dec={r.get('decode_tok_s')} total={r.get('total_s')} "
                  f"usage={r.get('usage')} qwait={r.get('queue_wait_ms')} err={r.get('error')} "
                  f"busy(run={m0.get('running')},wait={m0.get('waiting')})", flush=True)
            time.sleep(3)
        result["cases"][name] = {"runs": runs, "summary": summarize(runs)}

    def conc_case(name, n, reps):
        allr = []
        for rep in range(reps):
            out = [None] * n
            def go(j):
                out[j] = stream_one(a.base, key, a.model, make_prompt(50, rng), a.max_tokens, headers, a.temperature, a.timeout)
            m0 = vllm_metrics(a.metrics_url)
            t0 = time.perf_counter()
            ts = [threading.Thread(target=go, args=(j,)) for j in range(n)]
            for t in ts: t.start()
            for t in ts: t.join()
            wall = time.perf_counter() - t0
            toks = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in out if r)
            for r in out: r["vllm_at_start"] = m0; r["rep"] = rep
            allr.extend(out)
            agg = toks / wall if wall else None
            result["cases"].setdefault(name, {"reps": []})["reps"].append({"wall_s": wall, "agg_tok_s": agg, "tokens": toks})
            print(f"  {name} rep{rep}: wall={wall:.1f}s agg={agg:.2f} tok/s per-req dec="
                  f"{[round(r.get('decode_tok_s') or 0, 1) for r in out]} ttft={[round(r.get('ttft_s') or 0, 2) for r in out]} "
                  f"errs={[r.get('error') for r in out if r.get('error')]}", flush=True)
            time.sleep(5)
        result["cases"][name]["runs"] = allr
        result["cases"][name]["summary"] = summarize(allr)
        result["cases"][name]["summary"]["agg_tok_s_med"] = round(statistics.median(x["agg_tok_s"] for x in result["cases"][name]["reps"]), 2)

    try:
        if sel("short"): seq_case("short_50in", 50, a.reps)
        if sel("medium"): seq_case("medium_2k", 2000, a.reps)
        if sel("long"): seq_case("long_16k", 16000, a.long_reps)
        phase("mid")
        if sel("c2"): conc_case("conc2_short", 2, a.reps)
        if sel("c4"): conc_case("conc4_short", 4, max(1, a.reps - 1))
        if a.c8 and sel("c8"): conc_case("conc8_short", 8, 1)
    finally:
        phase("after-immediate"); time.sleep(10); phase("after+10s")
        stop.set(); time.sleep(1)
        result["rdma_after"] = {"gx10-01": rdma_counters(None), "gx10-02": rdma_counters(wssh) if wssh else None}
        for node in ("gx10-01", "gx10-02"):
            s = [x for x in samples if x["node"] == node]
            cu = cpu_util(s)
            result.setdefault("system", {})[node] = {
                "n": len(s),
                "mem_used_gib_min_max": [min((x["mem_used_gib"] for x in s), default=None), max((x["mem_used_gib"] for x in s), default=None)],
                "mem_avail_gib_min": min((x["mem_avail_gib"] for x in s), default=None),
                "swap_used_gib_max": max((x["swap_used_gib"] for x in s), default=None),
                "gpu_util_mean": round(statistics.mean([x["gpu_util"] for x in s if x["gpu_util"] is not None]), 1) if s else None,
                "cpu_util_mean": round(statistics.mean(cu), 1) if cu else None,
                "cpu_util_max": round(max(cu), 1) if cu else None,
                "first": {k: s[0][k] for k in ("mem_used_gib", "mem_avail_gib", "swap_used_gib", "gpu_util")} if s else None,
                "last": {k: s[-1][k] for k in ("mem_used_gib", "mem_avail_gib", "swap_used_gib", "gpu_util")} if s else None,
            }
        result["samples"] = samples
        result["rdma_delta"] = {n: {k: (result["rdma_after"][n].get(k, 0) - v) for k, v in (result["rdma_before"][n] or {}).items()
                                     if isinstance(v, int) and isinstance(result["rdma_after"][n].get(k), int)}
                                for n in ("gx10-01", "gx10-02") if result["rdma_before"].get(n)}
        result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(a.out, "w") as f:
            json.dump(result, f, indent=1, default=str)
        print("\nSUMMARY")
        for n, c in result["cases"].items():
            print(n, json.dumps(c["summary"]))
        print("system", json.dumps(result["system"]))
        print("rdma_delta(errors)", {n: {k: v for k, v in d.items() if "xmit" not in k and v} for n, d in result["rdma_delta"].items()})
        print("wrote", a.out)


if __name__ == "__main__":
    main()
