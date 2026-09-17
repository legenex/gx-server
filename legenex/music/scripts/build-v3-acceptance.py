#!/usr/bin/env python3
"""Build V3 (MUS) live acceptance for gx-music: the claims that need REAL audio.

Runs on gx10-02 against the gx-music supervisor (the sanctioned path: the
supervisor admits the job, takes the node lock and loads the engine; this script
never runs `docker run` for the GPU). It samples MemAvailable at 1 Hz for the
whole run, submits a fixed set of generations, downloads every WAV, and writes a
JSON evidence file. The acoustic measurements are made afterwards by
`audio-forensics.py` inside the engine image (CPU, offline).

Cases (the seed is held fixed inside each pair so only one thing changes):

  vocal_female / vocal_male  same lyrics, prompt and seed, only `vocal_intent`
  instrumental               same prompt and seed, `instrumental: true`
  tags_a / tags_b            same prompt, seed and length, only `style_tags`
  desc_and_prompt            `description` AND `prompt` together

    build-v3-acceptance.py --out-dir /srv/logs/acceptance/build-v3/mus
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LYRICS = """[Verse]
City lights are glowing in the rain
Every window holds a different name
I keep walking where the rivers meet
Hear the rhythm rising from the street

[Chorus]
Hold on, hold on, the night is young
Sing it loud until the morning comes
Hold on, hold on, we're never done
Dancing in the neon sun"""

VOCAL_PROMPT = "upbeat synth-pop song with a catchy chorus, bright analog synths, punchy drums"
GROOVE_PROMPT = "a steady four-on-the-floor groove"


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, headers=None, timeout=180):
        h = {"Authorization": f"Bearer {self.key}"}
        data = None
        if raw is not None:
            data, h["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                if (r.headers.get("Content-Type") or "").startswith("application/json"):
                    return r.status, json.loads(payload)
                return r.status, payload
        except urllib.error.HTTPError as e:
            body_ = e.read() or b"{}"
            try:
                return e.code, json.loads(body_)
            except ValueError:
                return e.code, {"raw": body_[:400].decode("utf-8", "replace")}

    def wait(self, job_id: str, timeout: float = 3600) -> dict:
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            _, job = self.call("GET", f"/v1/music/{job_id}")
            state = (job["status"], job["detail"])
            if state != last:
                print(f"    [{time.time() - t0:6.1f}s] {job['status']:20s} {job['detail']}", flush=True)
                last = state
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(2)
        raise TimeoutError(job_id)


def meminfo() -> dict:
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        if k in ("MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
            m[k] = round(int(v.split()[0]) / 1048576, 2)
    m["SwapUsed"] = round(m["SwapTotal"] - m["SwapFree"], 2)
    return m


class MemSampler(threading.Thread):
    """1 Hz MemAvailable + swap, written to a CSV as it goes (D-038 evidence)."""

    def __init__(self, csv_path: Path) -> None:
        super().__init__(daemon=True)
        self.csv = csv_path
        self.stop = False
        self.rows: list[tuple[float, float, float]] = []
        self.label = "idle"

    def run(self) -> None:
        with self.csv.open("w", encoding="utf-8") as fh:
            fh.write("epoch,mem_available_gib,swap_used_gib,label\n")
            while not self.stop:
                m = meminfo()
                t = time.time()
                self.rows.append((t, m["MemAvailable"], m["SwapUsed"]))
                fh.write(f"{t:.1f},{m['MemAvailable']},{m['SwapUsed']},{self.label}\n")
                fh.flush()
                time.sleep(1.0)

    def window(self, t0: float, t1: float) -> dict:
        sel = [r for r in self.rows if t0 <= r[0] <= t1]
        if not sel:
            return {}
        avail = [r[1] for r in sel]
        swap = [r[2] for r in sel]
        return {"samples": len(sel), "baseline_gib": avail[0], "min_gib": round(min(avail), 2),
                "max_gib": round(max(avail), 2), "end_gib": avail[-1],
                "growth_gib": round(avail[0] - min(avail), 2),
                "swap_used_start_gib": swap[0], "swap_used_max_gib": round(max(swap), 2)}


def download(c: Client, job: dict, index: int, dest: Path) -> dict:
    code, data = c.call("GET", f"/v1/music/{job['id']}/content?index={index}&format=wav", timeout=600)
    if code != 200:
        return {"ok": False, "status": code}
    dest.write_bytes(data)
    return {"ok": True, "path": str(dest), "bytes": len(data),
            "sha256": job["tracks"][index]["files"]["wav"]["sha256"]}


def run_case(c: Client, results: dict, sampler: MemSampler, name: str, path: str, body: dict,
             audio_dir: Path) -> dict | None:
    print(f"\n== {name}", flush=True)
    sampler.label = name
    t0 = time.time()
    code, job = c.call("POST", path, body)
    if code != 202:
        print(f"   SUBMIT FAILED {code}: {job}", flush=True)
        results["cases"][name] = {"ok": False, "submit_status": code, "response": job, "request": body}
        return None
    job = c.wait(job["id"])
    rec = {"job_id": job["id"], "status": job["status"], "wall_s": round(time.time() - t0, 1),
           "timings": job.get("timings"), "error": job.get("error"), "request_sent": body,
           "request_stored": job.get("request"), "mem": sampler.window(t0, time.time())}
    if job["status"] == "completed" and job.get("tracks"):
        t = job["tracks"][0]
        rec["track"] = {"index": t["index"], "seed": t.get("seed"), "bpm": t.get("bpm"), "key": t.get("key"),
                        "time_signature": t.get("time_signature"), "duration_s": t.get("duration_s"),
                        "caption": t.get("caption"), "lyrics": (t.get("lyrics") or "")[:400]}
        rec["wav"] = download(c, job, t["index"], audio_dir / f"{name}.wav")
        rec["ok"] = bool(rec["wav"].get("ok"))
    else:
        rec["ok"] = False
    print(f"   -> {job['status']} ok={rec['ok']} timings={job.get('timings')}", flush=True)
    results["cases"][name] = rec
    sampler.label = "idle"
    return job if rec["ok"] else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18820")
    ap.add_argument("--key-file", default="/srv/projects/gx-cluster/secrets/gx-music/api-key")
    ap.add_argument("--out-dir", default="/srv/logs/acceptance/build-v3/mus")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-unload", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    audio_dir = out / f"audio-{stamp}"
    audio_dir.mkdir(parents=True, exist_ok=True)
    c = Client(args.base, Path(args.key_file).read_text().strip())
    sampler = MemSampler(out / f"memwatch-node2-{stamp}.csv")
    sampler.start()
    time.sleep(3)  # a few idle baseline samples before anything is asked of the node

    results: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "base": args.base,
                     "audio_dir": str(audio_dir), "mem_before": meminfo(), "cases": {}}
    _, results["health_before"] = c.call("GET", "/health")
    _, results["model"] = c.call("GET", "/v1/music/model")
    only = set(filter(None, args.only.split(",")))

    def want(n: str) -> bool:
        return not only or n in only

    cold_t0 = time.time()
    if want("vocal_female"):
        run_case(c, results, sampler, "vocal_female", "/v1/music/generations", {
            "title": "MUS acceptance — female vocal", "prompt": VOCAL_PROMPT, "lyrics": LYRICS,
            "vocal_intent": "female", "vocal_language": "en", "style_tags": ["pop", "synthesizer", "80s"],
            "duration": 45, "bpm": 118, "key": "A minor", "time_signature": "4/4", "seed": 42,
            "thinking": True}, audio_dir)
        results["cold_load"] = sampler.window(cold_t0, time.time())
    if want("vocal_male"):
        run_case(c, results, sampler, "vocal_male", "/v1/music/generations", {
            "title": "MUS acceptance — male vocal", "prompt": VOCAL_PROMPT, "lyrics": LYRICS,
            "vocal_intent": "male", "vocal_language": "en", "style_tags": ["pop", "synthesizer", "80s"],
            "duration": 45, "bpm": 118, "key": "A minor", "time_signature": "4/4", "seed": 42,
            "thinking": True}, audio_dir)
    if want("instrumental"):
        run_case(c, results, sampler, "instrumental", "/v1/music/generations", {
            "title": "MUS acceptance — instrumental", "prompt": VOCAL_PROMPT, "instrumental": True,
            "style_tags": ["pop", "synthesizer", "80s"], "duration": 45, "bpm": 118, "key": "A minor",
            "time_signature": "4/4", "seed": 42, "thinking": True}, audio_dir)
    if want("tags_a"):
        run_case(c, results, sampler, "tags_a", "/v1/music/generations", {
            "title": "MUS acceptance — tags A", "prompt": GROOVE_PROMPT,
            "style_tags": ["deep house", "warm pads", "analog synth", "smooth"], "instrumental": True,
            "duration": 20, "bpm": 122, "key": "F minor", "time_signature": "4/4", "seed": 777,
            "thinking": False}, audio_dir)
    if want("tags_b"):
        run_case(c, results, sampler, "tags_b", "/v1/music/generations", {
            "title": "MUS acceptance — tags B", "prompt": GROOVE_PROMPT,
            "style_tags": ["death metal", "distorted electric guitar", "double kick", "aggressive"],
            "instrumental": True, "duration": 20, "bpm": 122, "key": "F minor", "time_signature": "4/4",
            "seed": 777, "thinking": False}, audio_dir)
    if want("desc_and_prompt"):
        run_case(c, results, sampler, "desc_and_prompt", "/v1/music/generations", {
            "title": "MUS acceptance — description + style prompt",
            "description": "a farewell to a harbour town at dawn",
            "prompt": "slow acoustic guitar, brushed drums, warm upright bass",
            "style_tags": ["folk", "acoustic"], "instrumental": True, "duration": 20, "seed": 555,
            "thinking": True}, audio_dir)

    # ------------------------------------------------ reference analysis (measured path)
    if want("reference"):
        src = results["cases"].get("desc_and_prompt") or results["cases"].get("tags_a")
        if src and src.get("wav", {}).get("ok"):
            wav = Path(src["wav"]["path"]).read_bytes()
            code, up = c.call("POST", "/v1/music/uploads", raw=wav,
                              headers={"X-Filename": "reference.wav"}, timeout=300)
            results["reference"] = {"upload_status": code, "upload": up}
            if code == 201:
                sampler.label = "reference"
                code, job = c.call("POST", "/v1/music/analyses",
                                   {"source": {"upload_id": up["id"]}, "understand": True})
                if code == 202:
                    done = c.wait(job["id"])
                    results["reference"]["analysis"] = {
                        "job_id": done["id"], "status": done["status"], "error": done.get("error"),
                        "analysis": done.get("analysis"), "timings": done.get("timings")}
                else:
                    results["reference"]["analysis"] = {"submit_status": code, "response": job}
                sampler.label = "idle"

    _, results["health_loaded"] = c.call("GET", "/health")
    results["mem_loaded"] = meminfo()
    results["engine_container_mem"] = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", "gx-music"],
        capture_output=True, text=True).stdout.strip() or None

    if not args.skip_unload:
        sampler.label = "unload"
        t0 = time.time()
        code, info = c.call("POST", "/v1/music/unload", {"if_idle": True}, timeout=600)
        time.sleep(20)
        ps = subprocess.run(["docker", "ps", "-a", "--filter", "name=^gx-music$", "--format", "{{.Names}}"],
                            capture_output=True, text=True).stdout.strip()
        ledger_path = Path("/srv/projects/gx-cluster/state/guard/node2-residency.json")
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        results["unload"] = {"status": code, "info": info, "seconds": round(time.time() - t0, 1),
                             "mem_after_20s": meminfo(), "container_left": ps or None,
                             "ledger_has_gx_music": "gx-music" in ledger}
        _, results["health_after_unload"] = c.call("GET", "/health")
        sampler.label = "idle"
        print("\n== unload:", json.dumps(results["unload"]), flush=True)

    time.sleep(3)
    sampler.stop = True
    time.sleep(1.5)
    results["mem_csv"] = str(sampler.csv)
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results["all_ok"] = all(v.get("ok") for v in results["cases"].values()) if results["cases"] else False
    dest = out / f"build-v3-acceptance-{stamp}.json"
    dest.write_text(json.dumps(results, indent=1))
    print(f"\nall_ok={results['all_ok']}  evidence -> {dest}\naudio -> {audio_dir}", flush=True)
    return 0 if results["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
