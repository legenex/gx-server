#!/usr/bin/env python3
"""Measure gx-image / gx-video memory growth on gx10-02 by job size (D-038).

Each job runs COLD on an otherwise idle node: ComfyUI is freed through the
router's own free path first, MemAvailable is sampled every second, and the
growth is (MemAvailable before) - (minimum during the job). The router's
admission still applies to every job, so a job is only started when its
current estimate keeps the 30 GiB reserve; sizes are chosen in safe steps.

    python3 legenex/tests/media_footprint_probe.py t2v:704x704:49 t2v:640x640:81 t2i:1328x1328:4

Job spec: kind:WxH:N where N is frames for t2v and the batch size for t2i.
Results: /srv/logs/acceptance/media-footprint-<UTC>/probe.json
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from reserve_live_acceptance import N2, ROUTER, Sampler, get, media_key, n2, router  # noqa: E402

RESERVE = 30.0


def router_multipart(path: str, fields: dict, file_field: str, filename: str, data: bytes) -> tuple[int, dict]:
    """POST a multipart form (used for image-to-video source uploads)."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n")
    body = ("".join(parts)).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(ROUTER + path, method="POST", data=body,
                                 headers={"Authorization": f"Bearer {media_key()}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else {"bytes": len(raw)}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def free_and_settle() -> float:
    n2("docker exec gx-media-router python -m gx_media_router.free_node >/dev/null 2>&1; sleep 6")
    best, stable = 0.0, 0
    for _ in range(60):
        avail = float(n2("awk '/^MemAvailable/{print $2/1048576}' /proc/meminfo") or 0)
        stable = stable + 1 if avail <= best + 0.25 else 0
        best = max(best, avail)
        if stable >= 3:
            break
        time.sleep(2)
    return best


def run(spec: str, out: Path) -> dict:
    kind, size, count = spec.split(":")
    w, h = (int(x) for x in size.split("x"))
    count_i = int(count)
    health = get(f"{ROUTER}/health")
    if health.get("busy") or health.get("waiting") or get("http://192.168.100.11:18820/health").get("engine") != "unloaded":
        raise SystemExit("gx10-02 is not idle (router busy/waiting or music loaded); not measuring")
    base = free_and_settle()
    samples = out / f"{kind}-{w}x{h}-{count_i}.tsv"
    sampler = Sampler(samples)
    t0 = time.time()
    try:
        if kind == "t2v":
            status, created = router("POST", "/v1/videos", {"model": "gx-video", "prompt": "TEST footprint: a slow "
                                     "pan across a harbour at sunset", "size": f"{w}x{h}", "length": count_i,
                                     "fps": 16, "seed": 11})
            vid = created.get("id")
            result = {}
            for _ in range(1800):
                _, result = router("GET", f"/v1/videos/{vid}")
                if result.get("status") in ("completed", "failed"):
                    break
                time.sleep(2)
            ok = result.get("status") == "completed"
            detail = {"frames": result.get("frames"), "error": result.get("error"), "cold": result.get("cold_start"),
                      "elapsed": result.get("elapsed_seconds"), "waiting": result.get("waiting")}
        elif kind == "i2v":
            # A small source image first, then image-to-video from it.
            _, img = router("POST", "/v1/images/generations", {"model": "gx-image", "prompt": "TEST footprint: a "
                              "lighthouse on a cliff", "size": "1024x1024", "n": 1, "seed": 11,
                              "response_format": "url"})
            src_url = (img.get("data") or [{}])[0].get("url")
            if not src_url:
                ok, detail = False, {"error": "no source image for i2v", "http": 0}
            else:
                png, fetch_http = b"", 0
                try:
                    req = urllib.request.Request(ROUTER + src_url,
                                                 headers={"Authorization": f"Bearer {media_key()}"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        png = r.read()
                        fetch_http = r.status
                except urllib.error.HTTPError as exc:
                    fetch_http = exc.code
                except urllib.error.URLError as exc:
                    fetch_http = -1
                    src_url = f"{src_url} ({exc.reason})"
                if fetch_http != 200 or not png:
                    ok, detail = False, {"error": "source image fetch failed", "http": fetch_http, "url": src_url}
                else:
                    status, created = router_multipart("/v1/videos", {"model": "gx-video", "prompt": "TEST footprint: "
                                                        "a slow pan across a harbour at sunset", "size": f"{w}x{h}",
                                                        "length": count_i, "fps": 16, "seed": 11},
                                                        "input_reference", "src.png", png)
                    vid = created.get("id")
                    result = {}
                    for _ in range(1800):
                        _, result = router("GET", f"/v1/videos/{vid}")
                        if result.get("status") in ("completed", "failed"):
                            break
                        time.sleep(2)
                    ok = result.get("status") == "completed"
                    detail = {"create_http": status, "frames": result.get("frames"), "error": result.get("error"),
                              "cold": result.get("cold_start"), "elapsed": result.get("elapsed_seconds"),
                              "waiting": result.get("waiting")}
        else:
            status, result = router("POST", "/v1/images/generations", {"model": "gx-image", "prompt": "TEST footprint:"
                                    " a lighthouse on a cliff", "size": f"{w}x{h}", "n": count_i, "seed": 11,
                                    "response_format": "url"})
            ok = status == 200
            detail = {"http": status, "images": len(result.get("data") or []), "error": result.get("error")}
        time.sleep(4)
    finally:
        mem = sampler.stop()
    growth = round(base - mem["min_mem_available_gib"], 1) if mem.get("samples") else None
    rec = {"job": spec, "megapixel_frames": round(w * h * count_i / 1e6, 3), "ok": ok, "seconds": round(time.time() - t0, 1),
           "baseline_gib": round(base, 1), "min_gib": mem.get("min_mem_available_gib"), "growth_gib": growth,
           "below_reserve_seconds": mem.get("below_reserve_seconds"), "swap_free_min_gib": mem.get("swap_free_min_gib"),
           **detail}
    print(json.dumps(rec), flush=True)
    return rec


def main() -> int:
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(f"/srv/logs/acceptance/media-footprint-{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for spec in sys.argv[1:]:
        try:
            results.append(run(spec, out))
        except Exception as exc:  # a failed job must not lose the other measurements
            rec = {"job": spec, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(rec), flush=True)
            results.append(rec)
            continue
        if results[-1].get("min_gib") is not None and results[-1]["min_gib"] < RESERVE + 4:
            print("stopping: the last job came within 4 GiB of the reserve", flush=True)
            break
    free_and_settle()
    (out / "probe.json").write_text(json.dumps(results, indent=2))
    print(f"-> {out}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
