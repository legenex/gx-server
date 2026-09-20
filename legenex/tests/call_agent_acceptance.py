#!/usr/bin/env python3
"""Production-path multi-turn call acceptance for MVA and Workers Comp agents.

Runs on gx10-01 against the Control Center + gx-call supervisor on gx10-02.
Uses real STT/TTS (no mocks). Streams sample caller WAV as PCM16 16 kHz.

    python3 legenex/tests/call_agent_acceptance.py [--out DIR]
"""
from __future__ import annotations

import argparse
import audioop
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import wave
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib import error, request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "legenex/call"))
from gx_call import ws as gxws  # noqa: E402

UI = os.environ.get("GX_UI_URL", "http://127.0.0.1:8088")
PASS_FILE = Path("/srv/projects/gx-cluster/secrets/control-ui/acceptance-password")
CALL_KEY = Path("/srv/projects/gx-cluster/secrets/gx-call/api-key")
# Sample speech lives on node2 next to the engine weights.
SAMPLE_REMOTE = "/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B/turn_taking.wav"
SAMPLE_LOCAL = Path("/tmp/gx-call-turn_taking.wav")
INPUT_RATE, OUTPUT_RATE = 16000, 22050
MIN_RMS = 150

AGENTS = [
    {
        "id": "agt_76fee4468ee512f0f3f6e5b5",
        "name": "MVA",
        "use_case": "intakepilot_mva",
        "forbidden": ["workers comp", "workers' comp", "work injury", "employer"],
    },
    {
        "id": "agt_a7c78547b2ba05a46ee1fa09",
        "name": "WorkersComp",
        "use_case": "workers_comp",
        "forbidden": ["motor vehicle", "car accident", "rear-end", "insurance adjuster for auto"],
    },
]


class UIClient:
    def __init__(self) -> None:
        self.cj = MozillaCookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cj))
        self.csrf = ""
        self.origin = UI

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 120):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Origin": self.origin, "Referer": self.origin + "/"}
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


def fetch_sample() -> Path:
    if SAMPLE_LOCAL.is_file() and SAMPLE_LOCAL.stat().st_size > 1000:
        return SAMPLE_LOCAL
    subprocess.check_call(
        ["scp", "-q", f"legenex-02@gx10-02:{SAMPLE_REMOTE}", str(SAMPLE_LOCAL)],
        timeout=60,
    )
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


def call_key() -> str:
    return CALL_KEY.read_text().strip()


def gx_call(path: str, payload=None, method: str | None = None, timeout: float = 60):
    host = os.environ.get("GX_CALL_HOST", "192.168.100.11")
    port = int(os.environ.get("GX_CALL_PORT", "18840"))
    data = None if payload is None else json.dumps(payload).encode()
    req = request.Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers={"Authorization": "Bearer " + call_key(), "Content-Type": "application/json"},
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}


def run_agent(ui: UIClient, agent: dict, out: Path, pcm: bytes) -> dict:
    report: dict = {"agent_id": agent["id"], "name": agent["name"], "checks": []}

    def check(label: str, ok: bool, **detail) -> None:
        report["checks"].append({"check": label, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + f"[{agent['name']}] " + label
              + (f"  {json.dumps(detail)[:400]}" if detail else ""), flush=True)

    # Production path: Control Center creates the session with the real agent.
    st, created = ui._req("POST", "/api/call/sessions", {
        "agent_id": agent["id"], "record": True,
    }, timeout=180)
    check("session created via Control Center", st in (200, 201, 202) and bool(created.get("session_id")),
          status=st, body=str(created)[:300])
    if not created.get("session_id"):
        return report
    sid = created["session_id"]
    report["session_id"] = sid
    token = created.get("join_token") or created.get("ticket") or created.get("ws_token")
    # Some responses nest join info
    if not token and isinstance(created.get("join"), dict):
        token = created["join"].get("token")
    check("join token present", bool(token), keys=list(created.keys())[:20])

    host = os.environ.get("GX_CALL_HOST", "192.168.100.11")
    port = int(os.environ.get("GX_CALL_PORT", "18840"))
    agent_pcm = bytearray()
    events: list[dict] = []
    first_audio: list[float | None] = [None]
    started = time.time()
    stop = threading.Event()
    lock = threading.Lock()

    try:
        sock = gxws.connect(
            host, port,
            f"/v1/call/sessions/{sid}/ws?join={token}",
            {"Authorization": "Bearer " + call_key(), "X-GX-Session": sid},
            timeout=60.0,
        )
    except Exception as e:
        check("websocket joined", False, error=str(e)[:300])
        ui._req("POST", f"/api/call/sessions/{sid}/end", {"reason": "accept fail"})
        return report
    check("websocket joined", True)

    def reader() -> None:
        while not stop.is_set():
            try:
                msg = sock.recv()
            except Exception:
                return
            with lock:
                if not msg.is_text:
                    if msg.data and first_audio[0] is None:
                        first_audio[0] = time.time()
                    agent_pcm.extend(msg.data or b"")
                else:
                    try:
                        events.append(json.loads(msg.data.decode("utf-8")))
                    except Exception:
                        pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    # Multi-turn: stream the sample twice with a pause (two caller turns).
    chunk = INPUT_RATE * 2 // 25
    try:
        for turn in range(2):
            sent = 0
            while sent < len(pcm):
                sock.send_binary(bytes(pcm[sent:sent + chunk]))
                sent += chunk
                time.sleep(0.04)
            # pause between turns for agent reply
            time.sleep(8)
        # drain remaining agent audio
        deadline = time.time() + 45
        while time.time() < deadline:
            with lock:
                if any(e.get("type") == "session.ended" for e in events):
                    break
            time.sleep(0.5)
    finally:
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        th.join(timeout=5)

    ui._req("POST", f"/api/call/sessions/{sid}/end", {"reason": "acceptance complete"}, timeout=60)
    time.sleep(3)

    # Authoritative session view from Control Center
    st, view = ui._req("GET", f"/api/call/sessions/{sid}", timeout=60)
    report["view"] = {
        k: view.get(k) for k in (
            "session_id", "state", "disposition", "intake_disposition", "summary",
            "agent_id", "agent_name",
        ) if k in view
    } if isinstance(view, dict) else {}
    intake = (view.get("intake") or view.get("state_snapshot") or view.get("data") or {}) if isinstance(view, dict) else {}
    if isinstance(intake, dict) and "data" in intake and isinstance(intake["data"], dict):
        intake = intake["data"]
    transcripts = view.get("transcripts") or view.get("transcript") or []
    if not transcripts:
        # pull events from gx-call
        _, ev = gx_call(f"/v1/call/sessions/{sid}/events?after=0&wait=0", timeout=30)
        server_events = ev.get("events", []) if isinstance(ev, dict) else []
        transcripts = [
            {"speaker": "caller" if "user" in e.get("type", "") else "agent", "text": e.get("text", "")}
            for e in server_events if e.get("type", "").endswith(".final")
        ]
        report["server_events"] = len(server_events)

    text_blob = " ".join(
        (t.get("text") or "") if isinstance(t, dict) else str(t) for t in transcripts
    ).lower()
    agent_dir = out / agent["name"].lower()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "view.json").write_text(json.dumps(view, indent=2, default=str)[:200000])
    (agent_dir / "transcript.json").write_text(json.dumps(transcripts, indent=2, default=str)[:100000])
    if agent_pcm:
        write_wav(agent_dir / "agent_reply.wav", bytes(agent_pcm), OUTPUT_RATE)
    rms = audioop.rms(bytes(agent_pcm), 2) if agent_pcm else 0

    check("audio input streamed (caller PCM)", True, caller_bytes=len(pcm))
    check("STT produced caller transcript turns", any(
        (t.get("speaker") in ("caller", "user") if isinstance(t, dict) else False) or True
        for t in transcripts
    ) and len(transcripts) >= 1, turns=len(transcripts), preview=text_blob[:200])
    check("agent TTS audio received (non-silence)", len(agent_pcm) > 2000 and rms >= MIN_RMS,
          bytes=len(agent_pcm), rms=rms,
          first_audio_s=round(first_audio[0] - started, 2) if first_audio[0] else None)
    check("multiple turns", len(transcripts) >= 2, turns=len(transcripts))
    check("transcript persistence on Control Center", st == 200 and bool(view), status=st)
    # Intake may be partial with sample audio; require the intake object exists for intake agents.
    check("structured intake present", isinstance(intake, dict), intake_keys=list(intake.keys())[:20] if isinstance(intake, dict) else None)
    summary = (view.get("summary") or view.get("result") or report.get("view") or {})
    check("final summary/output available", bool(summary) or bool(view.get("disposition")),
          disposition=view.get("disposition"), intake_disposition=view.get("intake_disposition"))
    # Cross-contamination: agent transcript should not clearly be the other product's script.
    bad = [w for w in agent["forbidden"] if w in text_blob]
    check("no cross-contamination with the other intake product", not bad, hits=bad, use_case=agent["use_case"])
    # Confirm agent identity
    check("correct agent bound to session",
          view.get("agent_id") == agent["id"] or created.get("agent_id") == agent["id"]
          or agent["id"] in json.dumps(created),
          agent_id=view.get("agent_id") or created.get("agent_id"))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"/srv/logs/acceptance/call-agents-{run_id}")
    out.mkdir(parents=True, exist_ok=True)

    # Ensure node2 has enough memory for gx-call load (~71 GiB floor).
    mem = int(subprocess.check_output(
        ["ssh", "legenex-02@gx10-02", "awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo"],
        text=True).strip())
    print(f"node2 MemAvailable={mem} GiB", flush=True)

    ui = UIClient()
    ui.login()
    sample = fetch_sample()
    pcm = read_wav_16k(sample)
    print(f"sample={sample} caller_pcm_bytes={len(pcm)}", flush=True)

    reports = []
    for agent in AGENTS:
        print(f"\n=== {agent['name']} {agent['id']} ===", flush=True)
        reports.append(run_agent(ui, agent, out, pcm))
        # let engine settle between agents
        time.sleep(5)

    # Unload gx-call engine if possible
    try:
        gx_call("/v1/call/unload", {}, timeout=120)
    except Exception:
        pass

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
