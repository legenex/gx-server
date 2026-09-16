#!/usr/bin/env python3
"""Real-generation acceptance for gx-image and gx-video through the LiteLLM gateway.

    gx_media_acceptance.py [--steps t2i,edit,variation,t2v,i2v,v2v] [--out DIR]

Every step generates real media and inspects it:
  * images: valid PNG, expected size, colour diversity, edit != source;
  * videos: ffprobe (codec, size, frame count, duration) and per-frame MD5
    (`framemd5`) to prove motion: a video of identical frames fails.

ffprobe/ffmpeg run in the local `linuxserver/ffmpeg` image with no network and
a read-only mount of the output directory. Outputs and a JSON report go to
/srv/logs/acceptance/media-<UTC>/ (outside Git).

Credentials: the gateway key is read server-side (see gx_tier_acceptance.py);
the variation step calls the node-2 router over the fabric with the media key,
exactly as the Control UI backend does. No key is printed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gx_tier_acceptance as tiers  # noqa: E402

GATEWAY = tiers.GATEWAY
ROUTER = os.environ.get("GX_MEDIA_ROUTER", "http://192.168.100.11:18800")
FFMPEG_IMAGE = os.environ.get("GX_FFMPEG_IMAGE", "linuxserver/ffmpeg:latest")


def env_value(name: str) -> str:
    env = tiers.REPO / "legenex" / "gateway" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for field, filename, ctype, data in files:
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                      f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def call(method: str, url: str, key: str, body: bytes | dict | None = None, ctype: str = "application/json",
         timeout: float = 3600) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}"})
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def png_info(data: bytes) -> dict:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    small = im.convert("RGB").resize((64, 64))
    colours = len(set(small.getdata()))
    return {"format": im.format, "width": im.width, "height": im.height, "colours_64px": colours}


def image_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-channel difference (0-255) at 64x64."""
    from PIL import Image, ImageChops

    ia = Image.open(io.BytesIO(a)).convert("RGB").resize((64, 64))
    ib = Image.open(io.BytesIO(b)).convert("RGB").resize((64, 64))
    diff = ImageChops.difference(ia, ib)
    return sum(sum(px) for px in diff.getdata()) / (64 * 64 * 3)


def ffprobe(path: Path) -> dict:
    cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{path.parent}:/m:ro",
           "--entrypoint", "ffprobe", FFMPEG_IMAGE, "-v", "error", "-select_streams", "v:0", "-count_frames",
           "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_frames,duration",
           "-show_entries", "format=duration,size,format_name", "-of", "json", f"/m/{path.name}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-300:]}
    return json.loads(out.stdout)


def distinct_frames(path: Path) -> tuple[int, int]:
    cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{path.parent}:/m:ro",
           "--entrypoint", "ffmpeg", FFMPEG_IMAGE, "-v", "error", "-i", f"/m/{path.name}",
           "-an", "-f", "framemd5", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    hashes = [line.split(",")[-1].strip() for line in out.stdout.splitlines()
              if line and not line.startswith("#")]
    return len(hashes), len(set(hashes))


def first_frame_png(path: Path) -> bytes:
    cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{path.parent}:/m:ro",
           "--entrypoint", "ffmpeg", FFMPEG_IMAGE, "-v", "error", "-i", f"/m/{path.name}",
           "-frames:v", "1", "-f", "image2", "-c:v", "png", "-"]
    return subprocess.run(cmd, capture_output=True, timeout=120).stdout


def check_video(path: Path) -> dict:
    probe = ffprobe(path)
    stream = (probe.get("streams") or [{}])[0]
    total, distinct = distinct_frames(path)
    frames = int(stream.get("nb_read_frames") or 0)
    ok = (stream.get("codec_name") == "h264" and frames >= 9 and distinct >= max(5, int(frames * 0.6)))
    return {"ok": ok, "codec": stream.get("codec_name"), "width": stream.get("width"),
            "height": stream.get("height"), "frames": frames, "fps": stream.get("r_frame_rate"),
            "duration": (probe.get("format") or {}).get("duration"), "bytes": path.stat().st_size,
            "framemd5_total": total, "framemd5_distinct": distinct}


def poll_video(video_id: str, key: str, budget: float = 3600) -> dict:
    deadline = time.time() + budget
    while time.time() < deadline:
        status, body, _ = call("GET", f"{GATEWAY}/videos/{video_id}", key, timeout=60)
        if status != 200:
            return {"status": "http_error", "http": status, "body": body[:300].decode("utf-8", "replace")}
        obj = json.loads(body)
        if obj.get("status") in ("completed", "failed"):
            return obj
        time.sleep(5)
    return {"status": "timeout"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="t2i,edit,variation,t2v,i2v,v2v")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    key = tiers.gateway_key()
    media_key = env_value("GX_MEDIA_API_KEY")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out or f"/srv/logs/acceptance/media-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"started": stamp, "steps": {}}

    def record(name: str, ok: bool, **info) -> None:
        report["steps"][name] = {"pass": bool(ok), **info}
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(info)[:400]}", flush=True)
        (out / "report.json").write_text(json.dumps(report, indent=2))

    source_png = (out / "t2i.png").read_bytes() if (out / "t2i.png").exists() else None
    t2v_path = out / "t2v.mp4"

    if "t2i" in steps:
        t0 = time.time()
        status, body, _ = call("POST", f"{GATEWAY}/images/generations", key, {
            "model": "gx-image", "size": "1024x1024", "seed": 424242,
            "prompt": "A photorealistic portrait of a man in a bright red shirt standing in a busy city street, daytime"})
        ok = status == 200
        info: dict = {"http": status, "seconds": round(time.time() - t0, 1)}
        if ok:
            payload = json.loads(body)
            import base64
            source_png = base64.b64decode(payload["data"][0]["b64_json"])
            (out / "t2i.png").write_bytes(source_png)
            info.update(png_info(source_png), gx=payload.get("gx"))
            ok = info["width"] == 1024 and info["height"] == 1024 and info["colours_64px"] > 500
        else:
            info["error"] = body[:400].decode("utf-8", "replace")
        record("t2i", ok, **info)

    if "edit" in steps and source_png:
        t0 = time.time()
        ctype, data = multipart(
            {"model": "gx-image", "prompt": "Change the background to a sunset beach and make the shirt black.",
             "response_format": "b64_json"},
            [("image", "source.png", "image/png", source_png)])
        status, body, _ = call("POST", f"{GATEWAY}/images/edits", key, data, ctype)
        info = {"http": status, "seconds": round(time.time() - t0, 1)}
        ok = status == 200
        if ok:
            import base64
            payload = json.loads(body)
            edited = base64.b64decode(payload["data"][0]["b64_json"])
            (out / "edit.png").write_bytes(edited)
            info.update(png_info(edited), diff_vs_source=round(image_diff(source_png, edited), 1),
                        gx=payload.get("gx"))
            ok = info["diff_vs_source"] > 8 and info["colours_64px"] > 500
            info["source_unchanged"] = (out / "t2i.png").read_bytes() == source_png
        else:
            info["error"] = body[:400].decode("utf-8", "replace")
        record("edit", ok, **info)

    if "variation" in steps and source_png:
        t0 = time.time()
        ctype, data = multipart({"response_format": "b64_json"}, [("image", "source.png", "image/png", source_png)])
        status, body, _ = call("POST", f"{ROUTER}/v1/images/variations", media_key, data, ctype)
        info = {"http": status, "seconds": round(time.time() - t0, 1), "path": "router (server-side, as the Control UI)"}
        ok = status == 200
        if ok:
            import base64
            payload = json.loads(body)
            var = base64.b64decode(payload["data"][0]["b64_json"])
            (out / "variation.png").write_bytes(var)
            info.update(png_info(var), diff_vs_source=round(image_diff(source_png, var), 1))
            ok = info["diff_vs_source"] > 3
        else:
            info["error"] = body[:400].decode("utf-8", "replace")
        record("variation", ok, **info)

    def run_video(name: str, url: str, body, ctype: str, dest: Path) -> dict | None:
        t0 = time.time()
        status, raw, _ = call("POST", url, key, body, ctype)
        if status not in (200, 202):
            record(name, False, http=status, error=raw[:400].decode("utf-8", "replace"))
            return None
        created = json.loads(raw)
        done = poll_video(created["id"], key)
        info = {"create_http": status, "id": created["id"], "status": done.get("status"),
                "workflow": done.get("workflow"), "seconds": round(time.time() - t0, 1),
                "remixed_from": done.get("remixed_from_video_id")}
        if done.get("status") != "completed":
            record(name, False, **info, error=done.get("error"))
            return None
        s2, content, headers = call("GET", f"{GATEWAY}/videos/{created['id']}/content", key, timeout=300)
        if s2 != 200:
            record(name, False, **info, content_http=s2)
            return None
        dest.write_bytes(content)
        check = check_video(dest)
        info.update(check)
        record(name, check["ok"], **info)
        return info

    if "t2v" in steps:
        run_video("t2v", f"{GATEWAY}/videos", {
            "model": "gx-video", "seconds": "3", "size": "640x640",
            "prompt": "A red sports car driving along a coastal road, camera tracking alongside, sunny day"},
            "application/json", t2v_path)

    if "i2v" in steps and source_png:
        ctype, data = multipart({"model": "gx-video", "seconds": "3", "size": "640x640",
                                 "prompt": "The man turns his head and smiles, the city traffic moves behind him"},
                                [("input_reference", "start.png", "image/png", source_png)])
        info = run_video("i2v", f"{GATEWAY}/videos", data, ctype, out / "i2v.mp4")
        if info:
            first = first_frame_png(out / "i2v.mp4")
            if first:
                report["steps"]["i2v"]["first_frame_diff_vs_source"] = round(image_diff(source_png, first), 1)

    if "v2v" in steps and t2v_path.exists():
        src_bytes = t2v_path.read_bytes()
        ctype, data = multipart({"model": "gx-video", "prompt": "make this scene take place at night, headlights on",
                                 "strength": "0.85"},
                                [("video", "source.mp4", "video/mp4", src_bytes)])
        info = run_video("v2v", f"{GATEWAY}/videos/edits", data, ctype, out / "v2v.mp4")
        if info:
            report["steps"]["v2v"]["source_unchanged"] = t2v_path.read_bytes() == src_bytes
            a, b = first_frame_png(t2v_path), first_frame_png(out / "v2v.mp4")
            if a and b:
                report["steps"]["v2v"]["first_frame_diff_vs_source"] = round(image_diff(a, b), 1)
            (out / "report.json").write_text(json.dumps(report, indent=2))

    passed = sum(1 for s in report["steps"].values() if s["pass"])
    print(f"SUMMARY media: {passed}/{len(report['steps'])} passed -> {out}/report.json")
    return 0 if passed == len(report["steps"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
