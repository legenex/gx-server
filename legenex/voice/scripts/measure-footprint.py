#!/usr/bin/env python3
"""Measure gx-voice's real memory footprint and latency on gx10-02 (1 Hz).

Runs ON gx10-02 against the running supervisor (127.0.0.1:18830). It never
starts a container itself: every load goes through the supervisor, i.e.
through the node-2 admission guard.

    python3 measure-footprint.py --out /srv/logs/acceptance/build-v3/voi/footprint-<ts>

Phases (each marked in phases.json against the 1 Hz mem.csv):
  baseline -> engine start -> for each variant: switch (cold page cache on the
  first pass), synthesize -> second pass (warm) -> unload -> settle.
Writes summary.json: MemAvailable at every mark, growth per phase, the peak,
the return after unload, engine start / variant load times and per-line
latency (generate_s, first_audio_s, rtf).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18830"
KEY = Path("/srv/projects/gx-cluster/secrets/gx-voice/api-key").read_text().strip()
TEXT = ("The quick brown fox jumps over the lazy dog. This sentence is here to measure how fast "
        "the voice model speaks on the GX10 cluster.")


def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None, timeout: float = 900):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Authorization": f"Bearer {KEY}",
               "Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return json.loads(payload) if r.headers.get("Content-Type", "").startswith("application/json") else payload
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read()[:400]!r}") from exc


def mem() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1048576, 2)
    return -1.0


def wait_job(job_id: str) -> dict:
    while True:
        job = call("GET", f"/v1/voice/jobs/{job_id}")
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--settle", type=float, default=8.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    health = json.loads(urllib.request.urlopen(f"{BASE}/health", timeout=5).read())
    if health.get("blocked_by"):
        raise SystemExit(f"refusing: {health['blocked_by']}")
    if health["state"] != "unloaded":
        call("POST", "/v1/voice/unload", {"if_idle": True})
        time.sleep(10)
    sampler = subprocess.Popen([str(HERE / "mem-sampler.sh"), str(out / "mem.csv"), "0", "1"])
    marks: list[dict] = []

    def mark(name: str, **extra) -> None:
        marks.append({"t": round(time.time(), 3), "phase": name, "mem_available_gib": mem(), **extra})
        print(json.dumps(marks[-1]), flush=True)
        (out / "phases.json").write_text(json.dumps(marks, indent=1))

    jobs: dict[str, dict] = {}
    try:
        time.sleep(args.settle)
        mark("baseline")
        t0 = time.time()
        snap = call("POST", "/v1/voice/load", {})
        mark("engine_ready", seconds=round(time.time() - t0, 1), last_load_seconds=snap.get("last_load_seconds"))
        time.sleep(args.settle)
        mark("engine_idle")
        ref_id = None
        for rnd in ("cold", "warm"):
            for variant in ("custom", "design", "base"):
                t1 = time.time()
                res = call("POST", "/v1/voice/load", {"variant": variant})
                mark(f"{rnd}:{variant}:loaded", seconds=round(time.time() - t1, 1),
                     variants=res.get("variants_loaded"))
                time.sleep(args.settle)
                mark(f"{rnd}:{variant}:resident")
                if variant == "custom":
                    voice = {"kind": "preset", "speaker": "aiden"}
                elif variant == "design":
                    voice = {"kind": "design", "description": "A calm, clear female narrator with a warm tone"}
                else:
                    voice = {"kind": "reference", "reference_id": ref_id, "transcript": TEXT}
                body = {"operation": {"custom": "tts", "design": "voice_design", "base": "voice_clone"}[variant],
                        "language": "english", "seed": 7, "title": f"footprint {rnd} {variant}",
                        "segments": [{"text": TEXT, "voice": voice}]}
                job = call("POST", "/v1/voice/jobs", body)
                done = wait_job(job["id"])
                jobs[f"{rnd}:{variant}"] = {"id": done["id"], "status": done["status"], "timings": done["timings"],
                                            "duration_s": (done["takes"] or [{}])[0].get("duration_s"),
                                            "error": done["error"]}
                mark(f"{rnd}:{variant}:generated", job=done["id"], status=done["status"])
                if variant == "design" and ref_id is None and done["status"] == "completed":
                    wav = call("GET", f"/v1/voice/jobs/{done['id']}/content?take=0&format=wav")
                    (out / "design-take.wav").write_bytes(wav)
                    ref_id = call("POST", "/v1/voice/references", raw=wav)["id"]
                if variant == "base" and ref_id is None:
                    raise SystemExit("no reference clip (the design job failed)")
        health = json.loads(urllib.request.urlopen(f"{BASE}/health", timeout=5).read())
        mark("before_unload", resident_gib=health["memory"]["resident_gib"],
             pending_gib=health["memory"]["pending_gib"])
        t2 = time.time()
        info = call("POST", "/v1/voice/unload", {})
        mark("unloaded", seconds=round(time.time() - t2, 1), container_gone=info.get("container_gone"))
        time.sleep(2 * args.settle)
        mark("settled")
    finally:
        time.sleep(1)
        sampler.terminate()
        sampler.wait(5)
    rows = list(csv.DictReader((out / "mem.csv").open()))
    series = [(float(r["epoch"]), float(r["mem_available_gib"])) for r in rows]

    def window_min(a: float, b: float) -> float | None:
        vals = [m for t, m in series if a <= t <= b]
        return min(vals) if vals else None

    by = {m["phase"]: m for m in marks}
    base = by["baseline"]["mem_available_gib"]
    summary = {
        "baseline_gib": base,
        "min_while_loaded_gib": window_min(by["engine_ready"]["t"] - 600, by["unloaded"]["t"]),
        "peak_growth_gib": round(base - (window_min(by["baseline"]["t"], by["unloaded"]["t"]) or base), 2),
        "engine_only_growth_gib": round(base - by["engine_idle"]["mem_available_gib"], 2),
        "resident_growth_gib": {k.split(":")[0] + ":" + k.split(":")[1]: round(base - m["mem_available_gib"], 2)
                                for k, m in by.items() if k.endswith(":resident")},
        "after_unload_gib": by["settled"]["mem_available_gib"],
        "returned_gib": round(by["settled"]["mem_available_gib"] - by["before_unload"]["mem_available_gib"], 2),
        "engine_start_s": by["engine_ready"]["seconds"],
        "variant_load_s": {k.replace(":loaded", ""): m["seconds"] for k, m in by.items() if k.endswith(":loaded")},
        "unload_s": by["unloaded"]["seconds"],
        "jobs": jobs,
        "samples": len(series),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
