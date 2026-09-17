#!/usr/bin/env python3
"""gx-music live acceptance: REAL ACE-Step generations through the service API.

Runs on node 2 (or anywhere that can reach the service). Writes a JSON evidence
file. Nothing here is mocked: every check downloads the produced audio and
inspects it.

    live-acceptance.py [--base http://127.0.0.1:18820] [--key-file ...] [--out evidence.json]
                       [--skip-unload]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gx_music.audio import analyze_wav, pcm_fingerprint, sniff  # noqa: E402

LYRICS = """[Verse]
City lights are glowing in the rain
Every window holds a different name
I keep walking where the rivers meet
Hear the rhythm rising from the street

[Chorus]
Hold on, hold on, the night is young
Sing it loud until the morning comes
Hold on, hold on, we're never done
Dancing in the neon sun

[Bridge]
Close your eyes and let it go
Feel the beat and let it grow

[Chorus]
Hold on, hold on, the night is young
Sing it loud until the morning comes"""


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, headers=None, timeout=120):
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
                if r.headers.get("Content-Type") == "application/json":
                    return r.status, json.loads(payload)
                return r.status, payload
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait(self, job_id: str, timeout: float = 3600) -> dict:
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            _, job = self.call("GET", f"/v1/music/{job_id}")
            state = (job["status"], job["detail"], job["progress"])
            if state != last:
                print(f"    [{time.time() - t0:6.1f}s] {job['status']:22s} {job['detail']} "
                      f"{'' if job['progress'] is None else round(job['progress'], 2)}", flush=True)
                last = state
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(2)
        raise TimeoutError(job_id)


class MemSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.min_avail = 1e9
        self.max_swap_used = 0.0
        self.max_engine_mem = ""
        self.stop = False

    def run(self) -> None:
        n = 0
        while not self.stop:
            m = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0]) / 1048576
            self.min_avail = min(self.min_avail, m["MemAvailable"])
            self.max_swap_used = max(self.max_swap_used, m["SwapTotal"] - m["SwapFree"])
            n += 1
            if n % 10 == 0:
                r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", "gx-music"],
                                   capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip():
                    self.max_engine_mem = max(self.max_engine_mem, r.stdout.strip().split(" /")[0],
                                              key=_to_gib)
            time.sleep(1)


def _to_gib(s: str) -> float:
    if not s:
        return 0.0
    units = {"KiB": 1 / 1048576, "MiB": 1 / 1024, "GiB": 1, "B": 1 / 1073741824}
    for u, f in units.items():
        if s.endswith(u):
            try:
                return float(s[: -len(u)]) * f
            except ValueError:
                return 0.0
    return 0.0


def meminfo() -> dict:
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        if k in ("MemAvailable", "SwapTotal", "SwapFree"):
            m[k] = round(int(v.split()[0]) / 1048576, 2)
    return m


def verify_track(c: Client, job: dict, index: int, tmp: Path, image: str) -> dict:
    out = {}
    for fmt in ("wav", "flac", "mp3"):
        code, data = c.call("GET", f"/v1/music/{job['id']}/content?index={index}&format={fmt}", timeout=300)
        assert code == 200, (fmt, code)
        p = tmp / f"{job['id']}-{index}.{fmt}"
        p.write_bytes(data)
        meta = job["tracks"][index]["files"][fmt]
        assert hashlib.sha256(data).hexdigest() == meta["sha256"], f"{fmt} sha mismatch"
        assert sniff(data[:16]) == fmt, f"{fmt} magic mismatch: {data[:8]!r}"
        probe = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "-v", f"{tmp}:/x:ro", "--entrypoint", "ffprobe",
             image, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", f"/x/{p.name}"],
            capture_output=True, text=True, timeout=120)
        assert probe.returncode == 0, probe.stderr
        info = json.loads(probe.stdout)
        s = info["streams"][0]
        out[fmt] = {"bytes": len(data), "codec": s["codec_name"], "sample_rate": int(s["sample_rate"]),
                    "channels": s["channels"], "duration_s": round(float(info["format"]["duration"]), 2),
                    "bit_rate": info["format"].get("bit_rate"), "sample_fmt": s.get("sample_fmt"),
                    "bits_per_raw_sample": s.get("bits_per_raw_sample")}
    w = analyze_wav(tmp / f"{job['id']}-{index}.wav")
    out["analysis"] = {"duration_s": w.duration_s, "sample_rate": w.sample_rate, "channels": w.channels,
                       "bits": w.bits, "encoding": w.encoding, "peak": w.peak, "rms_dbfs": w.rms_dbfs,
                       "silent": w.silent}
    out["pcm_fingerprint"] = pcm_fingerprint(tmp / f"{job['id']}-{index}.wav")
    assert not w.silent and w.duration_s >= 5, out["analysis"]
    assert w.sample_rate == 48000 and w.channels == 2
    return out


def run_case(c: Client, name: str, method_path: str, body: dict, tmp: Path, image: str, results: dict,
             expect_duration: tuple[float, float] | None = None) -> dict | None:
    print(f"\n== {name}", flush=True)
    t0 = time.time()
    code, job = c.call("POST", method_path, body)
    if code != 202:
        print(f"   SUBMIT FAILED {code}: {job}")
        results["cases"][name] = {"ok": False, "submit_status": code, "response": job}
        return None
    job = c.wait(job["id"])
    rec = {"job_id": job["id"], "status": job["status"], "wall_s": round(time.time() - t0, 1),
           "timings": job["timings"], "error": job["error"], "request": job["request"],
           "parent_job_id": job["parent_job_id"]}
    if job["status"] == "completed":
        rec["tracks"] = []
        for t in job["tracks"]:
            v = verify_track(c, job, t["index"], tmp, image)
            rec["tracks"].append({"index": t["index"], "seed": t["seed"], "bpm": t["bpm"], "key": t["key"],
                                  "time_signature": t["time_signature"], "caption": t["caption"][:300],
                                  "lyrics_head": (t["lyrics"] or "")[:120], "verify": v})
        d = rec["tracks"][0]["verify"]["analysis"]["duration_s"]
        if expect_duration:
            lo, hi = expect_duration
            rec["duration_ok"] = lo <= d <= hi
        rec["ok"] = rec.get("duration_ok", True)
    else:
        rec["ok"] = False
    print(f"   -> {job['status']} ok={rec['ok']} timings={job['timings']} err={job['error']}", flush=True)
    results["cases"][name] = rec
    return job if job["status"] == "completed" else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18820")
    ap.add_argument("--key-file", default="/srv/projects/gx-cluster/secrets/gx-music/api-key")
    ap.add_argument("--out", default="live-acceptance.json")
    ap.add_argument("--image", default="gx-music-engine:acestep15-ca1e85f-t214")
    ap.add_argument("--skip-unload", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    c = Client(args.base, Path(args.key_file).read_text().strip())
    tmp = Path(tempfile.mkdtemp(prefix="gxm-accept-"))
    sampler = MemSampler()
    sampler.start()
    results: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mem_before": meminfo(), "cases": {}}
    _, results["model"] = c.call("GET", "/v1/music/model")
    only = set(filter(None, args.only.split(",")))

    def want(n: str) -> bool:
        return not only or n in only

    seed = 1234
    inst = voc = None
    if want("instrumental"):
        inst = run_case(c, "instrumental", "/v1/music/generations", {
            "title": "Acceptance — instrumental", "prompt": "warm cinematic ambient piece with evolving synth pads and soft piano",
            "style_tags": ["ambient", "cinematic", "piano"], "instrumental": True, "duration": 30,
            "bpm": 80, "key": "D minor", "time_signature": "4/4", "seed": seed, "thinking": True}, tmp, args.image,
            results, (28, 32))
    if want("vocal"):
        voc = run_case(c, "vocal_lyrics", "/v1/music/generations", {
            "title": "Acceptance — vocal", "prompt": "upbeat synth-pop song with a catchy chorus",
            "style_tags": ["pop", "female vocals", "synthesizer", "80s"], "lyrics": LYRICS,
            "vocal_language": "en", "duration": 60, "bpm": 118, "key": "A minor", "seed": 42,
            "thinking": True}, tmp, args.image, results, (57, 63))
    if want("tags"):
        run_case(c, "style_tags_only", "/v1/music/generations", {
            "title": "Acceptance — tags only", "style_tags": ["jazz", "saxophone", "double bass", "brushed drums",
                                                               "smoky", "late night"],
            "instrumental": True, "duration": 30, "seed": 7}, tmp, args.image, results, (28, 32))
    if want("determinism"):
        a = run_case(c, "determinism_a", "/v1/music/generations", {
            "prompt": "minimal techno loop", "instrumental": True, "duration": 20, "seed": 777, "thinking": False,
            "bpm": 124, "key": "A minor", "time_signature": "4/4", "vocal_language": "unknown"},
            tmp, args.image, results)
        b = run_case(c, "determinism_b_same_seed", "/v1/music/generations", {
            "prompt": "minimal techno loop", "instrumental": True, "duration": 20, "seed": 777, "thinking": False,
            "bpm": 124, "key": "A minor", "time_signature": "4/4", "vocal_language": "unknown"},
            tmp, args.image, results)
        d = run_case(c, "determinism_c_other_seed", "/v1/music/generations", {
            "prompt": "minimal techno loop", "instrumental": True, "duration": 20, "seed": 778, "thinking": False,
            "bpm": 124, "key": "A minor", "time_signature": "4/4", "vocal_language": "unknown"},
            tmp, args.image, results)
        if a and b and d:
            fa = results["cases"]["determinism_a"]["tracks"][0]["verify"]["pcm_fingerprint"]
            fb = results["cases"]["determinism_b_same_seed"]["tracks"][0]["verify"]["pcm_fingerprint"]
            fd = results["cases"]["determinism_c_other_seed"]["tracks"][0]["verify"]["pcm_fingerprint"]
            results["determinism"] = {"same_seed_identical": fa == fb, "other_seed_different": fa != fd}
            if fa != fb:
                # Not bit-identical: measure how close (GPU kernels can be non-deterministic).
                import array
                import math
                def samples(jid):
                    raw = (tmp / f"{jid}-0.wav").read_bytes()
                    i = raw.index(b"data") + 8
                    arr = array.array("f"); arr.frombytes(raw[i:i + (len(raw) - i) // 4 * 4]); return arr
                sa, sb, sd = samples(a["id"]), samples(b["id"]), samples(d["id"])
                n = min(len(sa), len(sb), len(sd))
                def corr(x, y):
                    mx, my = sum(x[:n]) / n, sum(y[:n]) / n
                    num = sum((x[i] - mx) * (y[i] - my) for i in range(0, n, 7))
                    dx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(0, n, 7)))
                    dy = math.sqrt(sum((y[i] - my) ** 2 for i in range(0, n, 7)))
                    return num / (dx * dy) if dx and dy else 0.0
                results["determinism"]["corr_same_seed"] = round(corr(sa, sb), 4)
                results["determinism"]["corr_other_seed"] = round(corr(sa, sd), 4)
            print("   determinism:", results["determinism"])
    if want("description"):
        run_case(c, "description_mode", "/v1/music/generations", {
            "title": "Acceptance — description", "description": "a gentle acoustic folk song about the sea at dawn",
            "seed": 99}, tmp, args.image, results)
    if want("edits") and (inst or voc):
        base_job = voc or inst
        run_case(c, "remix_cover", "/v1/music/remix", {
            "title": "Acceptance — remix", "source": {"job_id": base_job["id"]},
            "prompt": "acoustic unplugged version, warm guitar and piano", "style_tags": ["acoustic", "folk"],
            "strength": 0.5, "seed": 5}, tmp, args.image, results)
        run_case(c, "repaint_edit", "/v1/music/edits", {
            "title": "Acceptance — repaint 10-20s", "source": {"job_id": base_job["id"]},
            "start": 10, "end": 20, "mode": "balanced", "strength": 0.6, "seed": 6}, tmp, args.image, results)
        ext_src = inst or voc
        src_dur = ext_src["tracks"][0]["duration_s"]
        run_case(c, "extend_end", "/v1/music/extend", {
            "title": "Acceptance — extend +20s", "source": {"job_id": ext_src["id"]}, "seconds": 20,
            "direction": "end", "seed": 8}, tmp, args.image, results, (src_dur + 17, src_dur + 23))
        # reference audio via upload
        wav = tmp / f"{ext_src['id']}-0.mp3"
        code, up = c.call("POST", "/v1/music/uploads", raw=wav.read_bytes(), headers={"X-Filename": "reference.mp3"})
        results["upload"] = {"status": code, "upload": up}
        if code == 201:
            run_case(c, "reference_audio_generation", "/v1/music/generations", {
                "title": "Acceptance — with reference", "prompt": "dreamy electronic track",
                "reference": {"upload_id": up["id"]}, "instrumental": True, "duration": 30, "seed": 11},
                tmp, args.image, results, (28, 32))
            run_case(c, "remix_of_upload", "/v1/music/remix", {
                "title": "Acceptance — remix of upload", "source": {"upload_id": up["id"]},
                "prompt": "lofi hip hop, dusty drums, mellow keys", "strength": 0.4, "seed": 12},
                tmp, args.image, results)
        _, lin = c.call("GET", f"/v1/music/{base_job['id']}/lineage")
        results["lineage"] = lin
    _, results["model_after"] = c.call("GET", "/v1/music/model")
    results["mem_min_available_gib"] = round(sampler.min_avail, 2)
    results["swap_max_used_gib"] = round(sampler.max_swap_used, 2)
    results["engine_peak_container_mem"] = sampler.max_engine_mem
    results["mem_loaded"] = meminfo()
    if not args.skip_unload:
        t0 = time.time()
        code, info = c.call("POST", "/v1/music/unload", timeout=300)
        time.sleep(10)
        ps = subprocess.run(["docker", "ps", "-a", "--filter", "name=^gx-music$", "--format", "{{.Names}}"],
                            capture_output=True, text=True).stdout.strip()
        ledger_path = Path("/srv/projects/gx-cluster/state/guard/node2-residency.json")
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        results["unload"] = {"status": code, "info": info, "seconds": round(time.time() - t0, 1),
                             "mem_after_10s": meminfo(), "container_left": ps or None,
                             "ledger_has_gx_music": "gx-music" in ledger}
        print("\n== unload:", json.dumps(results["unload"]))
    sampler.stop = True
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results["all_ok"] = all(v.get("ok") for v in results["cases"].values())
    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"\nall_ok={results['all_ok']}  evidence -> {args.out}  audio copies -> {tmp}")
    return 0 if results["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
