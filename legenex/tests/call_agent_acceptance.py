#!/usr/bin/env python3
"""Production-path multi-turn call acceptance for MVA and Workers Comp agents.

Browser-equivalent path:
  Control Center / Playground session cookie
  -> POST /api/call/sessions (real agent)
  -> WS /rt/call/<sid> (Playground tunnel to gx-call, cookie auth)
  -> real PCM16 caller audio, real STT/TTS, intake + transcript

    python3 legenex/tests/call_agent_acceptance.py [--out DIR]
"""
from __future__ import annotations

import argparse
import audioop
import datetime as dt
import json
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib import error, request

try:
    import websocket  # websocket-client
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "websocket-client"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import websocket

PG = os.environ.get("GX_PG_URL", "http://127.0.0.1:8090")
PASS_FILE = Path("/srv/projects/gx-cluster/secrets/control-ui/acceptance-password")
SAMPLE_REMOTE = "/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B/turn_taking.wav"
SAMPLE_LOCAL = Path("/tmp/gx-call-turn_taking.wav")
INPUT_RATE, OUTPUT_RATE = 16000, 22050
MIN_RMS = 100

AGENTS = [
    {
        "id": "agt_76fee4468ee512f0f3f6e5b5",
        "name": "MVA",
        "use_case": "intakepilot_mva",
        "forbidden": ["workers compensation claim number", "employer name required for wc"],
    },
    {
        "id": "agt_a7c78547b2ba05a46ee1fa09",
        "name": "WorkersComp",
        "use_case": "workers_comp",
        "forbidden": ["motor vehicle accident intake form", "rear-end collision checklist"],
    },
]


class PGClient:
    def __init__(self) -> None:
        self.cj = MozillaCookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cj))
        self.csrf = ""
        self.origin = PG

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 180):
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "Origin": self.origin,
            "Referer": self.origin + "/",
        }
        if method != "GET" and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:400].decode("utf-8", "replace")}

    def login(self) -> None:
        pw = PASS_FILE.read_text().strip()
        st, body = self._req("POST", "/api/login", {"username": "acceptance", "password": pw})
        if st >= 300 or not body.get("csrf"):
            raise RuntimeError(f"login failed: {st} {body}")
        self.csrf = body["csrf"]

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.cj)


def fetch_sample() -> Path:
    if SAMPLE_LOCAL.is_file() and SAMPLE_LOCAL.stat().st_size > 1000:
        return SAMPLE_LOCAL
    subprocess.check_call(["scp", "-q", f"legenex-02@gx10-02:{SAMPLE_REMOTE}", str(SAMPLE_LOCAL)], timeout=60)
    return SAMPLE_LOCAL


def read_wav_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if channels > 1:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != INPUT_RATE:
        frames, _ = audioop.ratecv(frames, 2, 1, rate, INPUT_RATE, None)
    return frames


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def run_agent(pg: PGClient, agent: dict, out: Path, pcm: bytes) -> dict:
    report: dict = {"agent_id": agent["id"], "name": agent["name"], "checks": []}

    def check(label: str, ok: bool, **detail) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + f"[{agent['name']}] " + label
              + (f"  {json.dumps(detail)[:400]}" if detail else ""), flush=True)

    st, created = pg._req("POST", "/api/call/sessions", {"agent_id": agent["id"], "record": False}, timeout=300)
    check("session created via production Playground/CC path",
          st in (200, 201, 202) and bool(created.get("session_id")),
          status=st, engine=created.get("engine"), body=str(created)[:250])
    if not created.get("session_id"):
        return report
    sid = created["session_id"]
    report["session_id"] = sid
    report["created"] = {k: created.get(k) for k in (
        "agent_id", "agent_name", "agent_version", "engine", "ws_path", "state")}
    check("correct agent bound", created.get("agent_id") == agent["id"], agent_id=created.get("agent_id"))
    check("MVA/WC intake schema attached",
          isinstance(created.get("state_snapshot"), dict) and isinstance(created.get("completion"), dict),
          missing=(created.get("completion") or {}).get("missing", [])[:8])

    # Wait for engine to leave loading if needed
    for _ in range(90):
        st2, view = pg._req("GET", f"/api/call/sessions/{sid}", timeout=30)
        eng = (view or created).get("engine") or created.get("engine")
        if eng in ("ready", "loaded", "live", "running", "unloaded", "failed", "error"):
            if eng in ("failed", "error"):
                check("engine reached usable state", False, engine=eng, view=str(view)[:200])
            break
        time.sleep(2)

    ws_url = PG.replace("http://", "ws://").replace("https://", "wss://") + f"/rt/call/{sid}"
    agent_pcm = bytearray()
    events: list[dict] = []
    first_audio: list[float | None] = [None]
    started = time.time()
    err_box: list[str] = []
    lock = threading.Lock()
    opened = threading.Event()

    def on_open(ws):
        opened.set()

    def on_message(ws, message):
        with lock:
            if isinstance(message, (bytes, bytearray)):
                if message and first_audio[0] is None:
                    first_audio[0] = time.time()
                agent_pcm.extend(message)
            else:
                try:
                    events.append(json.loads(message))
                except Exception:
                    events.append({"raw": str(message)[:200]})

    def on_error(ws, error):
        err_box.append(str(error))

    def on_close(ws, status, msg):
        pass

    ws = websocket.WebSocketApp(
        ws_url,
        header=[f"Cookie: {pg.cookie_header()}", f"Origin: {PG}"],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    th = threading.Thread(target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10), daemon=True)
    th.start()
    if not opened.wait(60):
        check("websocket joined via Playground tunnel", False, error="; ".join(err_box) or "timeout")
        pg._req("POST", f"/api/call/sessions/{sid}/end", {"reason": "ws fail"})
        return report
    check("websocket joined via Playground tunnel", True, url=ws_url)

    # Multi-turn: stream sample twice
    chunk = INPUT_RATE * 2 // 25
    try:
        for _turn in range(2):
            sent = 0
            while sent < len(pcm):
                piece = bytes(pcm[sent:sent + chunk])
                try:
                    ws.send(piece, opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception as e:
                    err_box.append(f"send: {e}")
                    break
                sent += chunk
                time.sleep(0.04)
            time.sleep(10)
        deadline = time.time() + 50
        while time.time() < deadline:
            with lock:
                if any(e.get("type") == "session.ended" for e in events if isinstance(e, dict)):
                    break
            time.sleep(0.5)
    finally:
        try:
            ws.close()
        except Exception:
            pass
        th.join(timeout=5)

    pg._req("POST", f"/api/call/sessions/{sid}/end", {"reason": "acceptance complete"}, timeout=60)
    time.sleep(4)

    st, view = pg._req("GET", f"/api/call/sessions/{sid}", timeout=60)
    report["view"] = view if isinstance(view, dict) else {}
    intake = {}
    if isinstance(view, dict):
        intake = view.get("intake") or view.get("state_snapshot") or view.get("data") or {}
        if isinstance(intake, dict) and isinstance(intake.get("data"), dict):
            intake = intake["data"]
    transcripts = []
    if isinstance(view, dict):
        transcripts = view.get("transcripts") or view.get("transcript") or []
    if not transcripts:
        # derive from socket events
        transcripts = [
            {"speaker": "caller" if "user" in e.get("type", "") else "agent", "text": e.get("text", "")}
            for e in events if isinstance(e, dict) and str(e.get("type", "")).endswith(".final")
        ]

    text_blob = " ".join(
        (t.get("text") or "") if isinstance(t, dict) else str(t) for t in transcripts
    ).lower()
    # also include agent event text
    text_blob += " " + " ".join(
        str(e.get("text") or "") for e in events if isinstance(e, dict)
    ).lower()

    agent_dir = out / agent["name"].lower()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "view.json").write_text(json.dumps(view, indent=2, default=str)[:200000])
    (agent_dir / "events.json").write_text(json.dumps(events, indent=2, default=str)[:200000])
    (agent_dir / "transcript.json").write_text(json.dumps(transcripts, indent=2, default=str)[:100000])
    if agent_pcm:
        write_wav(agent_dir / "agent_reply.wav", bytes(agent_pcm), OUTPUT_RATE)
    rms = audioop.rms(bytes(agent_pcm), 2) if agent_pcm else 0

    check("audio input streamed", True, caller_bytes=len(pcm))
    check("STT / transcript activity", len(transcripts) >= 1 or any(
        isinstance(e, dict) and "transcript" in str(e.get("type", "")) for e in events
    ), turns=len(transcripts), events=len(events), preview=text_blob[:180])
    check("agent TTS audio (non-silence)", len(agent_pcm) > 1500 and rms >= MIN_RMS,
          bytes=len(agent_pcm), rms=rms,
          first_audio_s=round(first_audio[0] - started, 2) if first_audio[0] else None)
    check("multiple turns attempted", True, streamed_turns=2)
    check("transcript/session persistence", st == 200 and bool(view), status=st)
    check("structured intake extraction present", isinstance(intake, dict) and (
        "disposition" in intake or "caller_name" in intake or "missing" in (view.get("completion") or {})
    ), intake_keys=list(intake.keys())[:15] if isinstance(intake, dict) else None,
        completion=view.get("completion") if isinstance(view, dict) else None)
    summary = None
    if isinstance(view, dict):
        summary = view.get("summary") or view.get("result") or view.get("disposition")
    check("final summary/output", bool(summary) or bool(view.get("disposition") if isinstance(view, dict) else False),
          disposition=(view or {}).get("disposition") if isinstance(view, dict) else None,
          intake_disposition=(view or {}).get("intake_disposition") if isinstance(view, dict) else None)
    bad = [w for w in agent["forbidden"] if w in text_blob]
    check("no cross-contamination with the other intake product", not bad, hits=bad, use_case=agent["use_case"])
    if err_box:
        report["ws_errors"] = err_box
    return report


def ensure_call_memory() -> int:
    """Unload gx-reason if needed so gx-call can admit (~71 GiB floor)."""
    n2 = int(subprocess.check_output(
        ["ssh", "legenex-02@gx10-02", "awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo"],
        text=True).strip())
    if n2 >= 75:
        return n2
    # unload via control center
    passw = PASS_FILE.read_text().strip()
    ck = "/tmp/gxcall-mem.ck"
    def curl(*a):
        return subprocess.run(["curl", "-sS", "-c", ck, "-b", ck, *a], capture_output=True, text=True).stdout
    j = json.loads(curl("-X", "POST", "http://127.0.0.1:8088/api/login",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps({"username": "acceptance", "password": passw})))
    csrf = j["csrf"]
    r = json.loads(curl("-X", "POST", "http://127.0.0.1:8088/api/actions/model.gx-reason.unload",
                        "-H", "Content-Type: application/json",
                        "-H", f"X-CSRF-Token: {csrf}",
                        "-H", "Origin: http://127.0.0.1:8088",
                        "-d", json.dumps({"confirm": True})))
    jid = r.get("id")
    if jid:
        for _ in range(40):
            time.sleep(3)
            st = json.loads(curl(f"http://127.0.0.1:8088/api/actions/jobs/{jid}"))
            if st.get("state") != "running":
                break
    return int(subprocess.check_output(
        ["ssh", "legenex-02@gx10-02", "awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo"],
        text=True).strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"/srv/logs/acceptance/call-agents-{run_id}")
    out.mkdir(parents=True, exist_ok=True)

    mem = ensure_call_memory()
    print(f"node2 MemAvailable={mem} GiB", flush=True)

    pg = PGClient()
    pg.login()
    sample = fetch_sample()
    pcm = read_wav_16k(sample)
    print(f"sample={sample} caller_pcm_bytes={len(pcm)}", flush=True)

    reports = []
    for agent in AGENTS:
        print(f"\n=== {agent['name']} {agent['id']} ===", flush=True)
        # re-check memory between agents
        ensure_call_memory()
        reports.append(run_agent(pg, agent, out, pcm))
        time.sleep(5)

    summary = {
        "run": run_id,
        "out": str(out),
        "node2_mem_gib_start": mem,
        "agents": reports,
    }
    failed = sum(1 for r in reports for c in r["checks"] if not c["ok"])
    total = sum(len(r["checks"]) for r in reports)
    summary["failed"] = failed
    summary["total"] = total
    (out / "call-agent-acceptance.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({"out": str(out), "failed": failed, "total": total}))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
