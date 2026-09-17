#!/usr/bin/env python3
"""Live gx10-02 acceptance for D-038: gx-video and gx-music never push normal
operation below the 30 GiB MemAvailable reserve.

Real generations only, through the sanctioned paths (the Control Center
creative queue and music integration as the loopback `acceptance` account,
and the node-2 media router API that the gateway uses). gx10-02
MemAvailable and SwapFree are sampled every second for the whole run.

  S0  preflight: Auto profile, no holds, node 2 idle
  S1  real music generation (cold engine load)
  S2  a video while idle gx-music is loaded: music is unloaded (verified), then the video runs
  S3  music submitted while a video runs: it waits with a specific reason, then runs
  S4  router API: a video next to PINNED music waits with its reason; after unpin the
      router unloads the idle engine (verified) and the video runs
  S5  a keyframe video edit is refused immediately (it can never keep the reserve)
  S6  cleanup: test assets deleted, pin removed, no holds left

    python3 legenex/tests/reserve_live_acceptance.py
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gx_ui_live_check import Session  # noqa: E402

REPO = HERE.parents[1]
N2 = "legenex-02@gx10-02"
ROUTER = "http://192.168.100.11:18800"
MUSIC = "http://192.168.100.11:18820"
RESERVE = 30.0
TAG = "TEST gx reserve"


def get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def media_key() -> str:
    for line in (REPO / "legenex/gateway/.env").read_text().splitlines():
        if line.startswith("GX_MEDIA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("GX_MEDIA_API_KEY missing")


def router(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(ROUTER + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {media_key()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else {"bytes": len(raw), "head": raw[:12]}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def n2(cmd: str, timeout: float = 30) -> str:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", N2, cmd], capture_output=True, text=True,
                          timeout=timeout).stdout


class Sampler:
    """1 Hz MemAvailable / SwapFree on gx10-02, plus router / music state changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", N2,
             "while :; do printf '%s %s %s\\n' \"$(date +%s)\" "
             "\"$(awk '/^MemAvailable/{print $2}' /proc/meminfo)\" "
             "\"$(awk '/^SwapFree/{print $2}' /proc/meminfo)\"; sleep 1; done"],
            stdout=path.open("w"), stderr=subprocess.DEVNULL)
        self.events: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._states, daemon=True)
        self._thread.start()

    def _states(self) -> None:
        last = None
        while not self._stop.is_set():
            try:
                r, m = get(f"{ROUTER}/health", 5), get(f"{MUSIC}/health", 5)
                cur = (r.get("held_by"), r.get("resident_alias"), len(r.get("waiting") or []), m.get("engine"),
                       m.get("busy"), m.get("active_jobs"))
                if cur != last:
                    self.events.append(f"{time.strftime('%H:%M:%S')} router held_by={cur[0]} resident={cur[1]} "
                                       f"waiting={cur[2]} | music engine={cur[3]} busy={cur[4]} active={cur[5]} "
                                       f"| pending router={r['memory'].get('pending_gib')} "
                                       f"music={(m.get('memory') or {}).get('pending_gib')}")
                    last = cur
            except (OSError, ValueError, KeyError):
                pass
            self._stop.wait(2)

    def stop(self) -> dict:
        self._stop.set()
        self.proc.terminate()
        rows = []
        for line in self.path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                rows.append((int(parts[0]), int(parts[1]) / 1048576, int(parts[2]) / 1048576))
        if not rows:
            return {"samples": 0}
        low = min(rows, key=lambda r: r[1])
        return {"samples": len(rows), "seconds": rows[-1][0] - rows[0][0],
                "min_mem_available_gib": round(low[1], 2),
                "min_at": dt.datetime.fromtimestamp(low[0]).strftime("%H:%M:%S"),
                "swap_free_start_gib": round(rows[0][2], 2), "swap_free_min_gib": round(min(r[2] for r in rows), 2),
                "swap_free_end_gib": round(rows[-1][2], 2),
                "below_reserve_seconds": sum(1 for r in rows if r[1] < RESERVE)}


def main() -> int:
    run = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(f"/srv/logs/acceptance/reserve-live-{run}")
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"run": run, "checks": [], "steps": {}}
    created_assets: list[str] = []

    def check(label: str, ok: bool, **info) -> bool:
        report["checks"].append({"check": label, "ok": bool(ok), **info})
        print(("PASS " if ok else "FAIL ") + label + (f"  {json.dumps(info, default=str)[:400]}" if info else ""),
              flush=True)
        return ok

    s = Session()

    def music_job(prompt: str, seconds: int = 15) -> str:
        j = s.call("POST", "/api/music/jobs", {"prompt": f"{TAG}: {prompt}", "style_tags": ["ambient"],
                                                "instrumental": True, "duration": seconds, "seed": 7,
                                                "batch_size": 1, "title": f"{TAG} {prompt}"}, expect=202)
        return j["id"]

    def wait_music(job_id: str, budget: float = 1200, on_status=None) -> dict:
        deadline, seen = time.time() + budget, []
        while time.time() < deadline:
            j = s.call("GET", f"/api/music/jobs/{job_id}", expect=200)
            key = (j.get("status"), j.get("detail"))
            if not seen or seen[-1] != key:
                seen.append(key)
                if on_status:
                    on_status(j)
            if j.get("status") in ("completed", "failed", "cancelled") and (j.get("status") != "completed"
                                                                            or j.get("imported")):
                j["statuses_seen"] = seen
                return j
            time.sleep(3)
        raise AssertionError(f"music job {job_id} timed out; seen {seen}")

    def video_job(prompt: str, **extra) -> str:
        j = s.call("POST", "/api/media/jobs", {"kind": "t2v", "prompt": f"{TAG}: {prompt}", "seconds": 2,
                                                "title": f"{TAG} {prompt}", **extra}, expect=202)
        return j["id"]

    def wait_video(job_id: str, budget: float = 1800, on_phase=None) -> dict:
        deadline, seen = time.time() + budget, []
        while time.time() < deadline:
            j = s.call("GET", f"/api/media/jobs/{job_id}", expect=200)
            key = (j["phase"], (j.get("waiting") or {}).get("reason") or j.get("detail"))
            if not seen or seen[-1] != key:
                seen.append(key)
                if on_phase:
                    on_phase(j)
            if j["phase"] in ("ready", "failed", "cancelled"):
                j["phases_seen"] = seen
                created_assets.extend(j.get("assets") or [])
                return j
            time.sleep(2)
        raise AssertionError(f"video job {job_id} timed out; seen {seen}")

    def music_engine() -> dict:
        m = get(f"{MUSIC}/health")
        m["container"] = n2("docker ps -a --filter name=^gx-music$ --format '{{.Names}} {{.Status}}'").strip()
        m["ledger_has_music"] = '"gx-music"' in n2("cat /srv/projects/gx-cluster/state/guard/node2-residency.json")
        return m

    # ------------------------------------------------------------------ S0
    r0, m0 = get(f"{ROUTER}/health"), music_engine()
    holds = n2("ls /srv/projects/gx-cluster/state/guard/").split()
    prof = json.loads(n2("cat /srv/projects/gx-cluster/state/guard/profile.json") or "{}").get("profile")
    avail0 = float(n2("awk '/^MemAvailable/{print $2/1048576}' /proc/meminfo") or 0)
    check("S0 preflight: router 2.4.0 idle, music supervisor 1.1.0, Auto profile, no holds",
          r0["version"] == "2.4.0" and not r0["busy"] and m0.get("version") == "1.1.0" and prof == "auto"
          and not [h for h in holds if h.endswith("-hold")], router_busy=r0["busy"], music=m0.get("engine"),
          profile=prof, guard_files=holds, mem_available_gib=round(avail0, 1))
    sampler = Sampler(out / "node2-mem.tsv")
    try:
        # -------------------------------------------------------------- S1
        t0 = time.time()
        mj = wait_music(music_job("S1 first track"))
        eng = music_engine()
        report["steps"]["S1"] = {"job": mj["id"], "status": mj["status"], "timings": mj.get("timings"),
                                 "seconds": round(time.time() - t0, 1), "engine": eng}
        check("S1 real music generation completed and was saved to the Library",
              mj["status"] == "completed" and bool(mj.get("library_assets")), timings=mj.get("timings"),
              assets=mj.get("library_assets"))
        created_assets.extend(mj.get("library_assets") or [])
        check("S1 engine is loaded and the supervisor measured its size",
              eng["engine"] == "ready" and eng["ledger_has_music"] and (eng["memory"] or {}).get("loaded_gib"),
              loaded_gib=(eng["memory"] or {}).get("loaded_gib"), pending_gib=(eng["memory"] or {}).get("pending_gib"))

        # -------------------------------------------------------------- S2
        engine_at_video_start: dict = {}

        def s2_phase(j: dict) -> None:
            if j["phase"] in ("generating", "loading") and not engine_at_video_start:
                engine_at_video_start.update(music_engine())

        vj = wait_video(video_job("S2 waves next to idle music"), on_phase=s2_phase)
        report["steps"]["S2"] = {"job": vj, "engine_when_video_started": engine_at_video_start,
                                 "router_last_eviction": get(f"{ROUTER}/health").get("last_eviction")}
        check("S2 video completed with real frames", vj["phase"] == "ready" and bool(vj.get("assets")),
              phases=vj["phases_seen"])
        check("S2 idle gx-music was unloaded BEFORE the video started (engine, container, ledger)",
              engine_at_video_start.get("engine") == "unloaded" and not engine_at_video_start.get("container")
              and not engine_at_video_start.get("ledger_has_music"),
              at_start={k: engine_at_video_start.get(k) for k in ("engine", "container", "ledger_has_music")})
        if vj.get("assets"):
            a = s.call("GET", f"/api/media/assets/{vj['assets'][0]}", expect=200)
            check("S2 the video has real motion", (a.get("frame_count") or 0) >= 9
                  and (a.get("distinct_frames") or 0) >= (a.get("frame_count") or 0) // 2,
                  frames=a.get("frame_count"), distinct=a.get("distinct_frames"))

        # -------------------------------------------------------------- S3
        started = threading.Event()
        vid3: dict = {}

        def run_video() -> None:
            vid3.update(wait_video(video_job("S3 lighthouse while music waits"),
                                   on_phase=lambda j: started.set() if j["phase"] == "generating" else None))

        th = threading.Thread(target=run_video)
        th.start()
        started.wait(900)
        time.sleep(3)
        waits: list[dict] = []
        m3 = wait_music(music_job("S3 track queued behind video", seconds=10),
                        on_status=lambda j: waits.append({"status": j.get("status"), "detail": j.get("detail")}))
        th.join(1800)
        report["steps"]["S3"] = {"video": vid3, "music": m3, "music_statuses": waits}
        waited = [w for w in waits if w["status"] == "waiting_for_resource"]
        check("S3 music submitted during the video waited with a specific reason",
              bool(waited) and any("gx-video" in (w["detail"] or "") or "gx10-02" in (w["detail"] or "")
                                   for w in waited), waiting=waited[:3])
        check("S3 video and music both completed", vid3.get("phase") == "ready" and m3["status"] == "completed",
              video=vid3.get("phase"), music=m3["status"])
        created_assets.extend(m3.get("library_assets") or [])

        # -------------------------------------------------------------- S4
        s.call("POST", "/api/resources/gx-music/pin", {}, expect=200)
        time.sleep(3)
        eng = music_engine()
        check("S4 music is loaded and pinned", eng["engine"] == "ready" and eng.get("pinned"), engine=eng["engine"],
              pinned=eng.get("pinned"))
        status, created = router("POST", "/v1/videos", {"model": "gx-video", "prompt": f"{TAG}: S4 city at dawn",
                                                        "seconds": "2", "size": "640x640", "seed": 3})
        vid = created.get("id")
        check("S4 router accepted the video (gateway API path)", status in (200, 202) and bool(vid), status=status)
        waiting = None
        for _ in range(60):
            status, j = router("GET", f"/v1/videos/{vid}")
            if j.get("phase") == "waiting":
                waiting = j
                break
            time.sleep(2)
        report["steps"]["S4_waiting"] = waiting
        w = (waiting or {}).get("waiting") or {}
        check("S4 the API shows WAITING with the blocker, numbers and next step",
              waiting is not None and waiting["status"] == "queued" and w.get("blocker") == "gx-music"
              and w.get("reserve_gib") == RESERVE and w.get("required_gib") and w.get("available_gib") is not None
              and "pinned" in (w.get("next") or ""), waiting=w)
        time.sleep(20)
        eng = music_engine()
        check("S4 pinned music was not unloaded while the video waited", eng["engine"] == "ready",
              engine=eng["engine"])
        s.call("POST", "/api/resources/gx-music/unpin", {}, expect=200)
        final = None
        for _ in range(900):
            status, j = router("GET", f"/v1/videos/{vid}")
            if j.get("status") in ("completed", "failed"):
                final = j
                break
            time.sleep(2)
        ev = get(f"{ROUTER}/health").get("last_eviction") or {}
        report["steps"]["S4_final"] = {"job": final, "eviction": ev}
        check("S4 after unpin the router unloaded the idle engine and verified the release",
              ev.get("requested") and ev.get("engine_unloaded") and ev.get("container_gone")
              and ev.get("ledger_clean"), eviction=ev)
        check("S4 the video then completed", (final or {}).get("status") == "completed",
              status=(final or {}).get("status"), error=(final or {}).get("error"))
        if final and final.get("status") == "completed":
            status, content = router("GET", f"/v1/videos/{vid}/content")
            check("S4 the video content downloads", status == 200 and content.get("bytes", 0) > 10_000,
                  bytes=content.get("bytes"))

        # -------------------------------------------------------------- S5
        if created_assets:
            src = next((a for a in created_assets if s.call("GET", f"/api/media/assets/{a}", expect=200)["type"]
                        == "video"), None)
            if src:
                kf = s.call("POST", "/api/media/jobs", {"kind": "v2v", "source_id": src, "strength": 0.85,
                                                         "prompt": f"{TAG}: make it night", "seconds": 2},
                            expect=202)
                t = time.time()
                kfj = wait_video(kf["id"], budget=120)
                check("S5 keyframe video edit fails fast with the reserve explanation (B-028)",
                      kfj["phase"] == "failed" and "137" in (kfj.get("error") or "") and time.time() - t < 60,
                      error=kfj.get("error"), seconds=round(time.time() - t, 1))
    finally:
        mem = sampler.stop()
        report["memory"] = mem
        report["state_changes"] = sampler.events
        # -------------------------------------------------------------- S6
        try:
            s.call("POST", "/api/resources/gx-music/unpin", {})
        except Exception:  # noqa: BLE001
            pass
        if created_assets:
            res = s.call("POST", "/api/media/delete", {"ids": sorted(set(created_assets)), "confirm": True})
            report["deleted_assets"] = res
    check("memory: gx10-02 MemAvailable never went below the 30 GiB reserve",
          mem.get("samples", 0) > 60 and mem.get("below_reserve_seconds") == 0 and mem["min_mem_available_gib"]
          >= RESERVE, **mem)
    check("memory: no pathological swap growth (< 2 GiB of swap taken)",
          mem.get("samples", 0) > 60 and mem["swap_free_start_gib"] - mem["swap_free_min_gib"] < 2.0,
          start=mem.get("swap_free_start_gib"), min=mem.get("swap_free_min_gib"))
    left = s.call("GET", f"/api/media/assets?q={TAG.replace(' ', '%20')}&limit=50", expect=200)
    check("cleanup: no test assets left in the Library", left.get("total", 0) == 0, total=left.get("total"))
    report["passed"] = all(c["ok"] for c in report["checks"])
    (out / "reserve-live.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\n{'ALL PASS' if report['passed'] else 'FAILURES'}: {sum(c['ok'] for c in report['checks'])}/"
          f"{len(report['checks'])} -> {out}")
    for e in sampler.events:
        print("  ", e)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
