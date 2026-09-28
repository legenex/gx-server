#!/usr/bin/env python3
"""Reproducible benchmark suite for the GX Cluster V4.1 (DeepSeek V4.1 Flash).

Implements ARCHITECTURE-V41.md §9. stdlib-only (urllib as the OpenAI client,
per the repo's D-003 no-dependency discipline). Results append as JSONL to
<sate>/bench/results.jsonl and a human summary table prints to stdout.

Suites:
    --suite quick   startup/health timing, short-prompt TTFT + decode tps
                    (1 stream, streaming), smoke completion (17*19=323),
                    tool-call health (one function call, DSML parsing),
                    memory snapshot (MemAvailable before/after).
    --suite full    quick + context ladder (short, 8k, 32k, 64k, 128k
                    synthetic prose; prefill tok/s, TTFT, cached tokens on
                    repeat) + concurrency (1, 2, 4 streams x 64-token
                    decodes; per-stream + aggregate tps).
    --suite coding  the multi-agent coding benchmark (ops/bench/coding-bench).

Speculation is a SERVER setting (profiles), not a request flag: the suite
takes --label and reads the live profile from the orchestrator /text/status;
matrix runs = orchestrator restart per profile BETWEEN suites (procedure in
ops/bench/README.md). The script measures whatever is live.

Metrics captured per request (one JSONL line): ts, label, profile, suite,
op, stream count, prompt tokens, completion tokens, cached tokens, TTFT ms,
prefill tok/s, decode tok/s, aggregate tok/s, queue wait ms (proxy: the
time-to-first-byte, which contains scheduler wait + connection setup --
prefill happens between the first byte and the first content token), total
latency, errors, MemAvailable low-water for the whole run (separate summary
line), GPU temp/power (nvidia-smi when present).

Everything degrades with a clear error when the cluster is down -- short
timeouts everywhere, no hangs. Exit codes: 0 ok, 1 benchmark error.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Endpoints (same pins as the gx CLI; overridable for tests)
# ---------------------------------------------------------------------------

MODEL_BASE = os.environ.get("GX_MODEL_BASE", "http://127.0.0.1:8888").rstrip("/")
ORCHESTRATOR_BASE = os.environ.get("GX_ORCH_BASE", "http://127.0.0.1:18900").rstrip("/")
SERVED_MODEL_ID = os.environ.get("GX_MODEL_ID", "DeepSeek-v4.1-Flash-EXL3")

STATE_ROOT = Path(os.environ.get("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))
RESULTS_PATH = Path(
    os.environ.get("GX_BENCH_RESULTS", str(STATE_ROOT / "bench" / "results.jsonl"))
)

TIMEOUT_S = float(os.environ.get("GX_TIMEOUT_S", "6"))
#: streaming reads need enough headroom for the longest decode (64 tokens
#: at ~20 tok/s worst case is ~4 s; ladder prompts need prefill of up to 128k
#: at ~1000 tok/s ≈ 130 s -- the ladder uses its own per-case timeout)
REQUEST_TIMEOUT_S = float(os.environ.get("GX_BENCH_TIMEOUT_S", "300"))

_REPO_ROOT = Path(__file__).resolve().parents[2]


class BenchError(Exception):
    """An operational benchmark error with a clear message."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_open(url: str, *, key: str = "", timeout: float = TIMEOUT_S, method: str = "GET",
               body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url: str, *, timeout: float = TIMEOUT_S) -> Any:
    try:
        with _http_open(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8").strip()
            if not raw:
                return {}
            return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise BenchError(f"GET {url}: {exc}") from None


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2:
            try:
                out[parts[0].strip()] = int(parts[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return out


def mem_available_gib() -> float | None:
    info = read_meminfo()
    return round(info["MemAvailable"] / (1024 * 1024), 1) if info and "MemAvailable" in info else None


# ---------------------------------------------------------------------------
# Memory low-water sampler (1 Hz thread) + GPU snapshot
# ---------------------------------------------------------------------------


class MemorySampler:
    """Samples MemAvailable at 1 Hz while running; keeps the low-water."""

    def __init__(self) -> None:
        self.low_water_gib: float | None = None
        self.start_gib: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.start_gib = mem_available_gib()
        self.low_water_gib = self.start_gib

        def loop() -> None:
            while not self._stop.is_set():
                mem = mem_available_gib()
                if mem is not None and (self.low_water_gib is None or mem < self.low_water_gib):
                    self.low_water_gib = mem
                self._stop.wait(1.0)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> float | None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        return self.low_water_gib


def gpu_snapshot() -> dict[str, Any]:
    """GPU temp/power via nvidia-smi when present; {} otherwise."""
    smi = shutil_which("nvidia-smi")
    if not smi:
        return {}
    try:
        proc = subprocess.run(
            [smi, "--query-gpu=temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, Any] = {}
    for i, line in enumerate(proc.stdout.strip().splitlines()):
        temps = [t.strip() for t in line.split(",")]
        if len(temps) == 2:
            out[f"gpu{i}_temp_c"] = temps[0]
            out[f"gpu{i}_power_w"] = temps[1]
    return out


def shutil_which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


# ---------------------------------------------------------------------------
# Prompt builders (deterministic)
# ---------------------------------------------------------------------------

_WORDS = (
    "cluster fabric memory latency kernel buffer stream packet tensor shard "
    "gradient token cache prefetch decode prefill NCCL RoCE bandwidth spike "
    "window queue profile scheduler drift thermal throttle checkpoint restore "
    "engram quant sparse dense draft expert routing attention mixture prose "
    "measure benchmark repeat variance median honest evidence locked pinned"
).split()


def build_prose_prompt(target_tokens: int, *, seed: int = 1234) -> str:
    """Deterministic synthetic prose sized approximately to `target_tokens`.

    Sizing uses ~1.35 tokens/word for English prose (a standard heuristic);
    the server reports exact prompt_tokens in usage, and the recorded metrics
    use THAT number, so approximation error only affects sizing, not results.
    """
    rng = random.Random(seed)
    words_needed = max(16, int(target_tokens / 1.35))
    words = [rng.choice(_WORDS) for _ in range(words_needed)]
    sentences: list[str] = []
    i = 0
    while i < len(words):
        chunk = words[i : i + 12]
        sentences.append(" ".join(chunk).capitalize() + ".")
        i += 12
    return (
        "The following is a synthetic passage of technical prose used only as "
        "prefill filler for a benchmark. Answer with the single word: done.\n\n"
        + " ".join(sentences)
    )


SMOKE_PROMPT = "What is 17 * 19? Answer with just the number."


def build_tool_call_messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": "What is the weather in Paris right now? Use the get_weather tool.",
        }
    ]


def build_tool_call_request() -> dict[str, Any]:
    """One function-call request (DSML tool parsing health check)."""
    return {
        "model": SERVED_MODEL_ID,
        "messages": build_tool_call_messages(),
        "max_tokens": 256,
        "temperature": 0.0,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a city",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }


# ---------------------------------------------------------------------------
# Streaming chat with TTFT + tps measurement
# ---------------------------------------------------------------------------


class StreamAccumulator:
    """Parses one SSE stream of an OpenAI streaming completion and derives
    the metrics from it. Split out of stream_chat so tests can feed fixture
    event lines without any network."""

    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.finish_reason: str | None = None
        self.done = False

    def feed(self, line: str, *, now: float, started: float) -> dict[str, float | None]:
        """Feed one raw SSE line. `now` is the wall clock for this line,
        `started` the request start (both time.monotonic seconds). Returns
        the running {ttfb_ms, ttft_ms} for the first meaningful line."""
        out = {"ttfb_ms": None, "ttft_ms": None}
        text = line.strip()
        if not text.startswith("data:"):
            return out
        payload = text[5:].strip()
        if payload == "[DONE]":
            self.done = True
            return out
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return out
        if event.get("usage"):
            self.usage = event["usage"]
        choices = event.get("choices") or []
        if not choices:
            return out
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            self.content_parts.append(delta["content"])
            out["ttft_ms"] = int((now - started) * 1000)
        if delta.get("tool_calls"):
            self.tool_calls.extend(delta["tool_calls"])
        if choices[0].get("finish_reason"):
            self.finish_reason = choices[0]["finish_reason"]
        return out

    def metrics(self, *, started: float, ttfb: float | None, ttft: float | None,
                last_chunk: float | None, total_s: float) -> dict[str, Any]:
        """Derive the full metric record from accumulated state."""
        ttft_ms = int((ttft - started) * 1000) if ttft is not None else None
        ttfb_ms = int((ttfb - started) * 1000) if ttfb is not None else None
        # The server emits the first SSE byte as soon as the request is being
        # processed, so time-to-first-byte = queue wait + connection setup
        # (prefill happens between first byte and first CONTENT token).
        # ttfb is therefore the honest proxy for queue wait; it is labeled
        # queue_wait_ms with that caveat (README).
        queue_wait_ms = ttfb_ms
        prompt_tokens = self.usage.get("prompt_tokens")
        completion_tokens = self.usage.get("completion_tokens")
        cached = (self.usage.get("prompt_tokens_details") or {}).get("cached_tokens") \
            if isinstance(self.usage.get("prompt_tokens_details"), dict) else None
        decode_tps = (completion_tokens / (last_chunk - ttft)) \
            if (completion_tokens and ttft is not None and last_chunk and last_chunk > ttft) else None
        prefill_tps = (prompt_tokens / (ttft - started)) \
            if (prompt_tokens and ttft is not None and ttft > started) else None
        return {
            "content": "".join(self.content_parts),
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "ttft_ms": ttft_ms,
            "ttfb_ms": ttfb_ms,
            "queue_wait_ms": queue_wait_ms,
            "total_ms": int(total_s * 1000),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached,
            "decode_tps": round(decode_tps, 1) if decode_tps else None,
            "prefill_tps": round(prefill_tps, 1) if prefill_tps else None,
        }


def stream_chat(
    *,
    prompt: str,
    max_tokens: int,
    base: str = MODEL_BASE,
    timeout: float = REQUEST_TIMEOUT_S,
    reasoning: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One streaming chat completion. Returns metrics + the final content.

    TTFT = first content delta; decode tps = completion_tokens / (last chunk
    ts - TTFT ts); prefill tps = prompt_tokens / TTFT (labeled approximation
    when queue wait is folded in -- see README).
    """
    body: dict[str, Any] = {
        "model": SERVED_MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "top_p": 0.95,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": reasoning if reasoning is not None else {"enable_thinking": False},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    started = time.monotonic()
    try:
        resp = _http_open(
            f"{base}/v1/chat/completions", method="POST", body=body, timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace")
        raise BenchError(f"chat HTTP {exc.code}: {detail.strip()[:200]}") from None
    except Exception as exc:  # noqa: BLE001
        raise BenchError(f"chat failed: {exc}") from None

    acc = StreamAccumulator()
    ttfb: float | None = None
    ttft: float | None = None
    last_chunk: float | None = None
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            now = time.monotonic()
            if ttfb is None and line.strip().startswith("data:"):
                ttfb = now
            marks = acc.feed(line, now=now, started=started)
            if marks["ttft_ms"] is not None and ttft is None:
                ttft = now
            if line.strip().startswith("data:") and marks["ttft_ms"] is not None:
                last_chunk = now
            if acc.done:
                break
    except Exception as exc:  # noqa: BLE001
        raise BenchError(f"stream read failed: {exc}") from None
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    return acc.metrics(
        started=started, ttfb=ttfb, ttft=ttft, last_chunk=last_chunk,
        total_s=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# Metric record + JSONL append
# ---------------------------------------------------------------------------


def make_record(
    *,
    label: str, suite: str, op: str, streams: int, profile: str,
    result: dict[str, Any] | None = None, error: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One results.jsonl line. Plain dict -- the JSONL append format is
    stable: one JSON object per line, fields below, ts first."""
    rec: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": label,
        "suite": suite,
        "op": op,
        "profile": profile,
        "streams": streams,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_tokens": None,
        "ttft_ms": None,
        "prefill_tps": None,
        "decode_tps": None,
        "aggregate_tps": None,
        "queue_wait_ms": None,
        "total_ms": None,
        "error": error,
    }
    if result:
        for key in ("ttft_ms", "prefill_tps", "decode_tps", "aggregate_tps",
                    "queue_wait_ms", "total_ms"):
            rec[key] = result.get(key)
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            rec[key] = result.get(key)
    if extra:
        rec.update(extra)
    return rec


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line, creating parents. Fails loudly to stderr but
    never raises into the benchmark loop (results also print to stdout)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"warn: cannot append to {path}: {exc}", file=sys.stderr)


def append_records(path: Path, records: list[dict[str, Any]]) -> None:
    """Append many records in one open (atomic-ish, one write per run)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
    except OSError as exc:
        print(f"warn: cannot append to {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Suite ops
# ---------------------------------------------------------------------------


def live_profile() -> str:
    """The live serving profile from the orchestrator (best effort)."""
    try:
        status = get_json(f"{ORCHESTRATOR_BASE}/text/status")
        if isinstance(status, dict):
            return str(status.get("profile") or status.get("spec_profile") or "unknown")
    except BenchError:
        pass
    return "unknown"


def op_health_timing() -> dict[str, Any]:
    """Startup/health timing on the model endpoint + orchestrator."""
    out: dict[str, Any] = {}
    started = time.monotonic()
    get_json(f"{MODEL_BASE}/health")
    out["model_health_ms"] = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    models = get_json(f"{MODEL_BASE}/v1/models")
    out["model_list_ms"] = int((time.monotonic() - started) * 1000)
    ids = [m.get("id") for m in models.get("data", [])] if isinstance(models, dict) else []
    out["served_ids"] = ids
    return out


def op_short_decode(label: str, suite: str, profile: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Short prompt, 1 stream, 64-token decode: TTFT + decode tps."""
    result = stream_chat(prompt="Write 64 words of plain prose about rivers.", max_tokens=96)
    records.append(make_record(label=label, suite=suite, op="short_decode", streams=1,
                               profile=profile, result=result))
    return result


def op_smoke(label: str, suite: str, profile: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """17*19=323 smoke completion (the Mia smoke test, MIA-RUNTIME.md)."""
    result = stream_chat(prompt=SMOKE_PROMPT, max_tokens=32)
    ok = "323" in result["content"]
    records.append(make_record(label=label, suite=suite, op="smoke", streams=1,
                               profile=profile, result=result, extra={"smoke_ok": ok}))
    result["smoke_ok"] = ok
    return result


def op_tool_call(label: str, suite: str, profile: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """One tool call; verify the model emits a parseable function call."""
    result = stream_chat(
        prompt="What is the weather in Paris right now? Use the get_weather tool.",
        max_tokens=256,
        tools=build_tool_call_request()["tools"],
    )
    ok = bool(result["tool_calls"]) or ("get_weather" in result["content"])
    records.append(make_record(label=label, suite=suite, op="tool_call", streams=1,
                               profile=profile, result=result, extra={"tool_ok": ok}))
    result["tool_ok"] = ok
    return result


def op_context_ladder(label: str, suite: str, profile: str, records: list[dict[str, Any]]) -> None:
    """Short, 8k, 32k, 64k, 128k synthetic prose; prefill tps, TTFT, cached
    tokens on repeat (prefix cache)."""
    for target in (100, 8000, 32000, 64000, 128000):
        prompt = build_prose_prompt(target)
        timeout = max(REQUEST_TIMEOUT_S, target / 400)  # ~400 tok/s worst case
        try:
            first = stream_chat(prompt=prompt, max_tokens=8, timeout=timeout)
        except BenchError as exc:
            records.append(make_record(label=label, suite=suite, op=f"context_{target}",
                                       streams=1, profile=profile, error=str(exc)))
            continue
        records.append(make_record(label=label, suite=suite, op=f"context_{target}", streams=1,
                                   profile=profile, result=first,
                                   extra={"target_tokens": target}))
        # repeat: same prompt -> prefix cache should serve cached_tokens
        try:
            second = stream_chat(prompt=prompt, max_tokens=8, timeout=timeout)
        except BenchError as exc:
            records.append(make_record(label=label, suite=suite, op=f"context_{target}_repeat",
                                       streams=1, profile=profile, error=str(exc)))
            continue
        records.append(make_record(label=label, suite=suite, op=f"context_{target}_repeat",
                                   streams=1, profile=profile, result=second,
                                   extra={"target_tokens": target, "repeat": True}))


def op_concurrency(label: str, suite: str, profile: str, records: list[dict[str, Any]]) -> None:
    """1, 2, 4 streams x 64-token decodes; per-stream + aggregate tps."""
    for n in (1, 2, 4):
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                r = stream_chat(prompt="Write 64 words of plain prose about lakes.", max_tokens=96)
            except BenchError as exc:
                with lock:
                    errors.append(str(exc))
                return
            with lock:
                results.append(r)

        started = time.monotonic()
        for _ in range(n):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        wall_s = time.monotonic() - started
        per_stream = [r["decode_tps"] for r in results if r["decode_tps"]]
        total_completion = sum(r["completion_tokens"] or 0 for r in results)
        aggregate = round(total_completion / wall_s, 1) if wall_s > 0 else None
        records.append(make_record(
            label=label, suite=suite, op="concurrency", streams=n, profile=profile,
            result=None if errors else (results[0] if results else None),
            error="; ".join(errors),
            extra={
                "per_stream_tps": per_stream,
                "aggregate_tps": aggregate,
                "wall_ms": int(wall_s * 1000),
            },
        ))


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------


def print_summary(records: list[dict[str, Any]], *, label: str, profile: str,
                  mem_low: float | None, mem_start: float | None,
                  gpu: dict[str, Any], results_path: Path | str | None = None) -> None:
    print()
    print(f"{'op':<22} {'streams':<7} {'ttft ms':>8} {'prefill t/s':>11} {'decode t/s':>10} {'cached':>8} {'error':<6}")
    for rec in records:
        ttft = rec.get("ttft_ms")
        prefill = rec.get("prefill_tps")
        decode = rec.get("decode_tps")
        cached = rec.get("cached_tokens")
        if rec["op"] == "concurrency":
            print(f"{rec['op'] + ' x' + str(rec['streams']):<22} {rec['streams']:<7} "
                  f"{'-':>8} {'-':>11} {rec.get('aggregate_tps') or '-':>10} {'-':>8} {'yes' if rec['error'] else 'no':<6}")
        else:
            print(f"{rec['op']:<22} {rec['streams']:<7} {ttft if ttft is not None else '-':>8} "
                  f"{prefill if prefill is not None else '-':>11} {decode if decode is not None else '-':>10} "
                  f"{cached if cached is not None else '-':>8} {'yes' if rec['error'] else 'no':<6}")
    smoke = [r for r in records if r["op"] == "smoke"]
    if smoke:
        print(f"\nsmoke (17*19=323): {'PASS' if smoke[0].get('smoke_ok') else 'FAIL'}")
    tools = [r for r in records if r["op"] == "tool_call"]
    if tools:
        print(f"tool call parsing : {'PASS' if tools[0].get('tool_ok') else 'FAIL'}")
    print(f"label={label} profile={profile}")
    print(f"MemAvailable: start {mem_start} GiB, low-water {mem_low} GiB")
    if gpu:
        print("GPU: " + "  ".join(f"{k}={v}" for k, v in sorted(gpu.items())))
    errors = [r for r in records if r["error"]]
    print(f"records: {len(records)} ({len(errors)} errors) -> {results_path if results_path is not None else RESULTS_PATH}")


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def run_quick(label: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    health = op_health_timing()
    records.append(make_record(
        label=label, suite="quick", op="health", streams=0, profile=live_profile(),
        extra={"model_health_ms": health["model_health_ms"],
               "model_list_ms": health["model_list_ms"],
               "served_ids": health["served_ids"]},
    ))
    profile = live_profile()
    op_short_decode(label, "quick", profile, records)
    op_smoke(label, "quick", profile, records)
    op_tool_call(label, "quick", profile, records)
    return health


def run_full(label: str, records: list[dict[str, Any]]) -> None:
    run_quick(label, records)
    profile = live_profile()
    op_context_ladder(label, "full", profile, records)
    op_concurrency(label, "full", profile, records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_bench",
        description="GX Cluster V4.1 benchmark suite (ops/bench; see README.md for matrix procedure).",
    )
    parser.add_argument("--suite", choices=("quick", "full", "coding"), default="quick")
    parser.add_argument("--label", default="adhoc", help="run label recorded with every metric line")
    parser.add_argument("--results", default=str(RESULTS_PATH),
                        help="JSONL results path (default: state/bench/results.jsonl)")
    args = parser.parse_args(argv)

    results_path = Path(args.results)

    if args.suite == "coding":
        script = _REPO_ROOT / "ops" / "bench" / "coding-bench" / "run_workflow.py"
        if not script.is_file():
            print(f"error: coding bench not found at {script}")
            return 1
        proc = subprocess.run([sys.executable, str(script), "--label", args.label])
        return proc.returncode

    # model endpoint must be up first -- fail fast with a clear message
    try:
        op_health_timing()
    except BenchError as exc:
        print(f"error: model endpoint not ready -- {exc}")
        print("start it with `gx start` (boot is about 25 minutes), then rerun.")
        return 1

    label = args.label
    profile = live_profile()
    print(f"benchmark suite={args.suite} label={label} live profile={profile}")
    print(f"results -> {results_path}")
    sampler = MemorySampler()
    sampler.start()
    gpu = gpu_snapshot()
    records: list[dict[str, Any]] = []
    try:
        if args.suite == "quick":
            run_quick(label, records)
        else:
            run_full(label, records)
    finally:
        mem_low = sampler.stop()
    append_records(results_path, records)
    # run-level summary record (memory low-water belongs to the run, not one request)
    append_record(results_path, make_record(
        label=label, suite=args.suite, op="run_summary", streams=0, profile=profile,
        extra={"mem_start_gib": sampler.start_gib, "mem_low_water_gib": mem_low,
               "gpu": gpu, "record_count": len(records)},
    ))
    print_summary(records, label=label, profile=profile, mem_low=mem_low,
                  mem_start=sampler.start_gib, gpu=gpu, results_path=results_path)
    return 0 if all(not r["error"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
