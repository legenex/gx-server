#!/usr/bin/env python3
"""Real Voice Studio acceptance against the live cluster (BUILD_V3 rule 10).

Runs ON gx10-01 and drives the **browser API** of GX-Playground
(`http://127.0.0.1:8090/api/voice/*`) as the loopback-only `acceptance`
account, so every capability is exercised through exactly the path the Voice
page uses: gx10-01 -> gx-voice supervisor on gx10-02 -> Qwen3-TTS engine.

    python3 live-acceptance.py --out /srv/logs/acceptance/build-v3/voi/live-<UTC>

Capabilities proved (one case each, all real generations):

  1  tts_preset          text-to-speech with a built-in preset speaker
  2  instructions_calm   emotion / style instructions, same voice + same seed ...
  3  instructions_urgent  ... so any difference in delivery comes from the words
  4  voice_design        a new voice from a text description
  5  saved_designed      the designed take saved as a voice and used again
  6  voice_clone         authorised cloning of an uploaded reference (+ consent)
  7  saved_cloned        the cloned voice saved and used again

It also checks output-format selection (WAV + MP3), job status transitions,
byte-range playback, the Library entry created by "Save to Library", and the
history row that the History page reads.

No audio is judged here: the takes are written to <out>/audio/ so that ffprobe
and `asr-check.py` (whisper-large-v3-turbo on the CPU, node 2) can verify them
independently. The password is read from the secrets file and never printed.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASSWORD_FILE = Path("/srv/projects/gx-cluster/secrets/control-ui/acceptance-password")
SENTENCE = ("The quick brown fox jumps over the lazy dog, "
            "while seven bright stars rise above the harbour wall.")
DESIGN = ("A warm middle-aged British female narrator with a calm, "
          "measured delivery and a slight smile in her voice")


class Fail(Exception):
    pass


class Session:
    """Cookie + CSRF session on the Playground, exactly like the browser."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        body = self.call("POST", "/api/login", {"username": "acceptance",
                                                "password": PASSWORD_FILE.read_text().strip()}, expect=200)
        self.csrf = body["csrf"]

    def _once(self, method: str, path: str, data: bytes | None, ctype: str, headers: dict, timeout: float):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Origin", self.base)
        if data is not None:
            req.add_header("Content-Type", ctype)
        if self.csrf and method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", ""), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", ""), dict(exc.headers)

    def call(self, method: str, path: str, body=None, *, raw: bytes | None = None,
             ctype: str = "application/json", headers: dict | None = None, expect: int | None = None,
             timeout: float = 900):
        """One request. Idempotent reads survive a Control Center restart (502/504)
        with a bounded, backed-off retry; writes are never replayed."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        attempts = 6 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                status, payload, ct, hdrs = self._once(method, path, data, ctype, headers or {}, timeout)
            except OSError as exc:
                if attempt + 1 >= attempts:
                    raise Fail(f"{method} {path}: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if status in (502, 503, 504) and attempt + 1 < attempts:
                print(f"    (retrying {method} {path} after HTTP {status})", flush=True)
                time.sleep(2 * (attempt + 1))
                if status == 502:
                    self._relogin()
                continue
            break
        self.last_status, self.last_headers = status, hdrs
        if expect is not None and status != expect:
            raise Fail(f"{method} {path}: HTTP {status}, expected {expect}: {payload[:400]!r}")
        return json.loads(payload or b"null") if "json" in ct else payload

    def _relogin(self) -> None:
        """A Control Center restart drops in-memory sessions; sign in again."""
        try:
            status, payload, ct, _ = self._once(
                "POST", "/api/login",
                json.dumps({"username": "acceptance",
                            "password": PASSWORD_FILE.read_text().strip()}).encode(),
                "application/json", {}, 30)
            if status == 200 and "json" in ct:
                self.csrf = json.loads(payload)["csrf"]
        except OSError:
            pass


def wav_facts(path: Path) -> dict:
    """Parse the RIFF header and compute the RMS ourselves (no dependency)."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise Fail(f"{path.name} is not a RIFF/WAVE file")
    pos, fmt, samples = 12, None, b""
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        chunk = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", chunk[:16])
        elif cid == b"data":
            samples = chunk
        pos += 8 + size + (size & 1)
    if fmt is None or not samples:
        raise Fail(f"{path.name} has no fmt/data chunk")
    _, channels, rate, _, _, bits = fmt
    if bits != 16:
        raise Fail(f"{path.name}: expected 16-bit PCM, got {bits}")
    n = len(samples) // 2
    if n == 0:
        raise Fail(f"{path.name} contains no audio samples")
    values = struct.unpack(f"<{n}h", samples[:n * 2])
    total = sum(float(v) * v for v in values)
    rms = (total / n) ** 0.5 / 32768.0
    peak = max(abs(v) for v in values) / 32768.0
    import math
    return {"bytes": len(data), "channels": channels, "sample_rate": rate, "bits": bits,
            "duration_s": round(n / channels / rate, 3),
            "rms_dbfs": round(20 * math.log10(rms), 2) if rms > 0 else -999.0,
            "peak": round(peak, 4)}


class Run:
    def __init__(self, s: Session, out: Path) -> None:
        self.s = s
        self.out = out
        self.audio = out / "audio"
        self.audio.mkdir(parents=True, exist_ok=True)
        self.cases: list[dict] = []
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}", flush=True)
        return bool(ok)

    def submit(self, label: str, body: dict) -> dict:
        t0 = time.time()
        job = self.s.call("POST", "/api/voice/jobs", body, expect=202)
        seen = {job["status"]}
        while job["status"] not in ("completed", "failed", "cancelled"):
            if time.time() - t0 > 1200:
                raise Fail(f"{label}: job {job['id']} did not finish in 20 minutes")
            time.sleep(1.5)
            job = self.s.call("GET", f"/api/voice/jobs/{job['id']}", expect=200)
            seen.add(job["status"])
        job["_statuses_seen"] = sorted(seen)
        job["_wall_s"] = round(time.time() - t0, 2)
        if job["status"] != "completed":
            raise Fail(f"{label}: job {job['id']} ended {job['status']}: {job.get('error')}")
        return job

    def fetch_take(self, job: dict, take: int, fmt: str, name: str) -> Path:
        blob = self.s.call("GET", f"/api/voice/jobs/{job['id']}/takes/{take}/audio?format={fmt}", expect=200)
        dest = self.audio / f"{name}.{fmt}"
        dest.write_bytes(blob)
        if not dest.stat().st_size:
            raise Fail(f"{name}.{fmt} is zero bytes")
        return dest

    def case(self, name: str, job: dict, *, formats=("wav",), expect_notes: bool | None = None) -> dict:
        files = {}
        for fmt in formats:
            path = self.fetch_take(job, 0, fmt, name)
            files[fmt] = str(path)
        take = job["takes"][0]
        facts = wav_facts(Path(files["wav"]))
        rec = {"case": name, "job_id": job["id"], "operation": job["operation"], "status": job["status"],
               "statuses_seen": job["_statuses_seen"], "wall_s": job["_wall_s"], "timings": job["timings"],
               "voice_id": job.get("voice_id"), "notes": job.get("notes") or [],
               "reported": {k: take.get(k) for k in ("seed", "duration_s", "sample_rate", "rms_dbfs", "peak")},
               "wav_header": facts, "files": files, "text": job["request"].get("text"),
               "instructions": job["request"].get("instructions"),
               "description": job["request"].get("description")}
        self.cases.append(rec)
        self.check(f"{name}: audio is non-empty", facts["bytes"] > 44, f"{facts['bytes']} bytes")
        self.check(f"{name}: audio is not silent", facts["rms_dbfs"] > -50, f"RMS {facts['rms_dbfs']} dBFS")
        self.check(f"{name}: duration is plausible", facts["duration_s"] >= 2.0,
                   f"{facts['duration_s']} s @ {facts['sample_rate']} Hz")
        self.check(f"{name}: job dict duration matches the file",
                   abs((take.get("duration_s") or 0) - facts["duration_s"]) < 0.25,
                   f"job {take.get('duration_s')} s vs file {facts['duration_s']} s")
        self.check(f"{name}: job dict sample rate matches the file",
                   take.get("sample_rate") == facts["sample_rate"], str(take.get("sample_rate")))
        if expect_notes is not None:
            has = any("not applied" in str(n).lower() for n in (job.get("notes") or []))
            self.check(f"{name}: the job says the instructions were NOT applied (Base model)" if expect_notes
                       else f"{name}: instructions were applied (no 'not applied' note)",
                       has is expect_notes, json.dumps(job.get("notes") or []))
        return rec


def main() -> int:  # noqa: C901 - a linear acceptance script
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    s = Session(args.base)
    r = Run(s, out)
    print(f"signed in to {args.base}; evidence -> {out}", flush=True)

    model = s.call("GET", "/api/voice/model", expect=200)
    r.check("model endpoint reachable (no 503)", model.get("alias") == "gx-voice",
            f"node={model.get('node')} state={model.get('state')}")
    speakers = [sp["id"] for sp in model.get("speakers") or []]
    r.check("preset speakers published", len(speakers) >= 3, ", ".join(speakers[:12]))

    # ---------------------------------------------------------------- 1  TTS
    print("\n1) text-to-speech with a preset voice", flush=True)
    preset = "preset:ryan" if "ryan" in speakers else f"preset:{speakers[0]}"
    job1 = r.submit("tts_preset", {"operation": "tts", "voice_id": preset, "text": SENTENCE,
                                   "language": "english", "seed": 1001, "title": "VOI acceptance preset"})
    c1 = r.case("tts_preset", job1, formats=("wav", "mp3"), expect_notes=False)
    r.check("tts_preset: MP3 variant produced",
            Path(c1["files"]["mp3"]).stat().st_size > 1000 and Path(c1["files"]["mp3"]).read_bytes()[:3] in
            (b"ID3", b"\xff\xfb", b"\xff\xf3"), f"{Path(c1['files']['mp3']).stat().st_size} bytes")
    r.check("tts_preset: status passed through generating",
            any(st in c1["statuses_seen"] for st in ("generating", "loading_model", "queued", "processing")),
            ", ".join(c1["statuses_seen"]))

    # byte-range playback, the way an <audio> element seeks
    full = Path(c1["files"]["wav"]).stat().st_size
    part = s.call("GET", f"/api/voice/jobs/{job1['id']}/takes/0/audio?format=wav",
                  headers={"Range": "bytes=0-1023"})
    r.check("playback: byte-range request served", s.last_status == 206 and len(part) == 1024,
            f"HTTP {s.last_status}, {len(part)} of {full} bytes")

    # --------------------------------------------- 2+3  emotion / style words
    print("\n2+3) emotion and style instructions (same voice, same seed)", flush=True)
    calm = r.submit("instructions_calm", {
        "operation": "tts", "voice_id": preset, "text": SENTENCE, "language": "english", "seed": 2002,
        "instructions": "Speak slowly and very calmly, almost whispering, with long thoughtful pauses.",
        "title": "VOI acceptance calm"})
    c_calm = r.case("instructions_calm", calm, expect_notes=False)
    urgent = r.submit("instructions_urgent", {
        "operation": "tts", "voice_id": preset, "text": SENTENCE, "language": "english", "seed": 2002,
        "instructions": "Shout this urgently and very fast, as if warning someone of immediate danger.",
        "title": "VOI acceptance urgent"})
    c_urgent = r.case("instructions_urgent", urgent, expect_notes=False)
    d_dur = round(c_calm["wav_header"]["duration_s"] - c_urgent["wav_header"]["duration_s"], 3)
    d_rms = round(c_urgent["wav_header"]["rms_dbfs"] - c_calm["wav_header"]["rms_dbfs"], 2)
    same_bytes = Path(c_calm["files"]["wav"]).read_bytes() == Path(c_urgent["files"]["wav"]).read_bytes()
    r.check("instructions change the delivery (identical seed and voice)",
            not same_bytes and (abs(d_dur) >= 0.4 or abs(d_rms) >= 1.5),
            f"calm {c_calm['wav_header']['duration_s']} s / {c_calm['wav_header']['rms_dbfs']} dBFS vs "
            f"urgent {c_urgent['wav_header']['duration_s']} s / {c_urgent['wav_header']['rms_dbfs']} dBFS "
            f"(delta {d_dur} s, {d_rms} dB)")

    # -------------------------------------------------------- 4  voice design
    print("\n4) voice design from a description", flush=True)
    design = r.submit("voice_design", {"operation": "voice_design", "description": DESIGN, "text": SENTENCE,
                                       "language": "english", "seed": 3003,
                                       "title": "VOI acceptance design"})
    r.case("voice_design", design)

    # ------------------------------------------------- 5  save and reuse it
    print("\n5) save the designed take as a voice and use it again", flush=True)
    vname = f"VOI acceptance narrator {int(started)}"
    designed = s.call("POST", "/api/voice/voices", {"kind": "designed", "name": vname,
                                                    "job_id": design["id"], "take": 0,
                                                    "description": DESIGN}, expect=201)
    r.check("designed voice saved", designed.get("kind") == "designed" and designed["id"].startswith("vc_"),
            f"{designed['id']} v{designed.get('version')}")
    listed = s.call("GET", "/api/voice/voices", expect=200)["voices"]
    r.check("saved voice appears in the voice list",
            any(v["id"] == designed["id"] for v in listed), f"{len(listed)} voices")
    reuse = r.submit("saved_designed", {"operation": "tts", "voice_id": designed["id"],
                                        "text": "This line proves a saved voice can be used again.",
                                        "instructions": "Sound delighted and excited.",
                                        "language": "english", "seed": 4004,
                                        "title": "VOI acceptance saved designed"})
    r.case("saved_designed", reuse, expect_notes=True)

    # ------------------------------------------------ 6  authorised cloning
    print("\n6) authorised reference voice cloning", flush=True)
    ref_wav = Path(c1["files"]["wav"]).read_bytes()
    asset = s.call("POST", "/api/voice/upload", raw=ref_wav, ctype="audio/wav",
                   headers={"X-Filename": "voi-acceptance-reference.wav",
                            "X-Title": "VOI acceptance reference"}, expect=200)
    r.check("reference upload accepted", str(asset.get("id", "")).startswith("a_"),
            f"{asset.get('id')} {asset.get('duration')} s")
    consent = {"confirmed": True,
               "statement": "Acceptance run: this recording is the cluster's own synthetic output "
                            "(job " + job1["id"] + "); the operator authorises cloning it."}
    clone = r.submit("voice_clone", {
        "operation": "voice_clone", "text": "Cloned from an authorised reference recording.",
        "reference": {"asset_id": asset["id"], "transcript": SENTENCE, "consent": consent},
        "language": "english", "seed": 5005, "title": "VOI acceptance clone"})
    r.case("voice_clone", clone, expect_notes=None)
    r.check("clone recorded a consent id", bool(clone.get("consent_id")), str(clone.get("consent_id")))
    refused = s.call("POST", "/api/voice/jobs", {
        "operation": "voice_clone", "text": "This must be refused.",
        "reference": {"asset_id": asset["id"], "transcript": SENTENCE},
        "language": "english"})
    r.check("cloning without consent is refused", s.last_status == 403,
            f"HTTP {s.last_status} {json.dumps(refused)[:160]}")

    print("\n7) save the cloned voice and use it again", flush=True)
    cloned = s.call("POST", "/api/voice/voices", {
        "kind": "cloned", "name": f"VOI acceptance clone {int(started)}",
        "reference_asset_id": asset["id"], "transcript": SENTENCE, "consent": consent,
        "description": "Cloned from the acceptance reference clip."}, expect=201)
    r.check("cloned voice saved with a consent record",
            cloned.get("kind") == "cloned" and bool(cloned.get("consent_id")),
            f"{cloned['id']} consent={cloned.get('consent_id')}")
    reuse2 = r.submit("saved_cloned", {"operation": "tts", "voice_id": cloned["id"],
                                       "text": "The saved cloned voice speaks this second line.",
                                       "instructions": "Sound delighted and excited.",
                                       "language": "english", "seed": 6006,
                                       "title": "VOI acceptance saved clone"})
    r.case("saved_cloned", reuse2, expect_notes=True)

    # ------------------------------------------------- Library and history
    print("\n8) Library entry and history row", flush=True)
    saved_asset = s.call("POST", f"/api/voice/jobs/{job1['id']}/takes/0/save",
                         {"title": "VOI acceptance preset take"}, expect=200)
    r.check("take saved to the Library",
            saved_asset.get("type") == "audio" and saved_asset.get("source_kind") == "voice_take",
            f"{saved_asset.get('id')} op={saved_asset.get('operation')} ref={saved_asset.get('source_ref')}")
    again = s.call("POST", f"/api/voice/jobs/{job1['id']}/takes/0/save", {}, expect=200)
    r.check("saving the same take again is idempotent", again.get("id") == saved_asset.get("id"),
            str(again.get("id")))
    lib = s.call("GET", "/api/media/assets?type=audio&limit=50", expect=200)
    items = lib.get("assets") or lib.get("items") or lib.get("data") or []
    r.check("Library lists the saved take", any(i.get("id") == saved_asset.get("id") for i in items),
            f"{len(items)} audio assets")
    jobs = s.call("GET", "/api/voice/jobs?limit=50", expect=200)["jobs"]
    ids = {j["id"] for j in jobs}
    mine = [c["job_id"] for c in r.cases]
    r.check("history lists every acceptance job", all(j in ids for j in mine),
            f"{len(ids)} rows, {len(mine)} of mine")

    health = json.loads(urllib.request.urlopen("http://192.168.100.11:18830/health", timeout=10).read())
    summary = {
        "started_at": started, "finished_at": time.time(),
        "seconds": round(time.time() - started, 1),
        "base": args.base, "node2_health": health,
        "model": {k: model.get(k) for k in ("alias", "node", "family", "licence", "state", "version")},
        "cases": r.cases, "checks": r.checks,
        "voices": {"designed": designed["id"], "cloned": cloned["id"]},
        "library_asset": saved_asset.get("id"),
        "passed": sum(1 for c in r.checks if c["ok"]), "failed": sum(1 for c in r.checks if not c["ok"]),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    cases = [{"file": Path(c["files"]["wav"]).name,
              "text": c["text"] or SENTENCE, "language": "english"} for c in r.cases]
    (r.audio / "cases.json").write_text(json.dumps(cases, indent=1))
    print(f"\n{summary['passed']} checks passed, {summary['failed']} failed; "
          f"{len(r.cases)} real generations in {summary['seconds']} s")
    print(f"evidence: {out}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as exc:
        print(f"ACCEPTANCE FAILED: {exc}", file=sys.stderr)
        sys.exit(2)
